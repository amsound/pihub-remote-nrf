
/*
 * PiHub BLE HID remote
 * NCS v3.2.1 / Zephyr 4.2.x
 *
 * Behaviour contract:
 *  - Advertise as a bondable HID-over-GATT keyboard + consumer-control peripheral
 *  - Stop advertising while connected; restart advertising on disconnect
 *  - Use a single bonded host model with an explicit unpair path
 *  - Accept hot-path HID commands over USB CDC ACM
 *  - Show advertising/connected/error state on the onboard LEDs
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/ring_buffer.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/logging/log.h>
#include <errno.h>

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/printk.h>
#include <ctype.h>

/* Forward declarations */
static int send_consumer_report(const uint8_t report2[2]);
static int send_keyboard_report(const uint8_t report8[8]);
static void set_leds_adv(void);
static void set_leds_conn(void);
static void pihub_led_err_on(void);
static void start_advertising(void);
static const char *phy_to_str(uint8_t phy);
static void update_link_ready(const char *why);
static void bond_clear_work_handler(struct k_work *work);
static void button_changed(uint32_t button_state, uint32_t has_changed);

/* Optional USB CDC ACM console support (logs over USB). */
#if IS_ENABLED(CONFIG_USB_DEVICE_STACK)
#include <zephyr/usb/usb_device.h>
#endif

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/settings/settings.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>

#include <dk_buttons_and_leds.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/hwinfo.h>
#include <string.h>

/*
 * NOTE (NCS v3.2.1 / Zephyr 4.2.x):
 * Advertising helper macros have changed signatures across Zephyr/NCS versions.
 * In particular, BT_LE_ADV_CONN_NAME can be defined in a way that no longer
 * matches the BT_LE_ADV_PARAM() signature (leading to the “passed 6 arguments”
 * error you were seeing).
 *
 * To keep this project buildable, we avoid those convenience macros and create
 * the advertising parameters explicitly.
 */

/* --------------------------------------------------------------------------
 * Logical LED channels
 *
 * Keep state logic board-agnostic by mapping semantic roles here.
 *
 * Nordic nRF52840 Dongle / E104-BT5040U physical LEDs:
 *   - single-colour LED     = P0.06
 *   - RGB red channel       = P0.08
 *   - RGB green channel     = P1.09
 *   - RGB blue channel      = P0.12
 *
 * Current DK mapping observed on this firmware/board combo:
 *   - DK_LED1 = dedicated red LED
 *   - DK_LED2 = RGB red
 *   - DK_LED3 = RGB green
 *   - DK_LED4 = RGB blue
 *
 * If another dongle maps these differently, only change the defines below.
 * -------------------------------------------------------------------------- */
#define PIHUB_LED_ERR   DK_LED1   /* red */
#define PIHUB_LED_CONN  DK_LED3   /* green */
#define PIHUB_LED_ADV   DK_LED4   /* blue */

/*
 * Advertising parameters
 *
 * Zephyr option bit names changed over time (e.g. CONNECTABLE vs CONN, USE_NAME
 * being removed in some versions). To keep this portable across NCS releases,
 * we:
 *   1) Use only the “connectable” option bit (whatever it is called), and
 *   2) Put the device name in the scan response (already done in `sd[]`),
 *      instead of relying on BT_LE_ADV_OPT_USE_NAME.
 */

/* 0x0020 = 20 ms, 0x0030 = 30 ms (units of 0.625 ms) */
#define PIHUB_ADV_INT_MIN 0x0020 /* 20 ms */
#define PIHUB_ADV_INT_MAX 0x0030 /* 30 ms */

/* Compatibility shim for different Zephyr versions */
#if !defined(BT_LE_ADV_OPT_CONN) && defined(BT_LE_ADV_OPT_CONNECTABLE)
#define BT_LE_ADV_OPT_CONN BT_LE_ADV_OPT_CONNECTABLE
#endif

static const struct bt_le_adv_param pihub_adv_param =
	BT_LE_ADV_PARAM_INIT(BT_LE_ADV_OPT_CONN,
				  PIHUB_ADV_INT_MIN,
				  PIHUB_ADV_INT_MAX,
				  NULL);

#define PIHUB_ADV_PARAM (&pihub_adv_param)

LOG_MODULE_REGISTER(pihub_hogp, LOG_LEVEL_INF);

#ifndef DEVICE_NAME
#define DEVICE_NAME CONFIG_BT_DEVICE_NAME
#endif

#ifndef FW_VERSION_STR
#ifdef CONFIG_APP_VERSION
#define FW_VERSION_STR CONFIG_APP_VERSION
#else
#define FW_VERSION_STR "1.0.0"
#endif
#endif


/* --- Simple HOGP: HID Service + a single input report characteristic --- */

/* HID Information (bcdHID=0x0111, country=0, flags=0x02 (normally remote wake)) */
/* HID Information (bcdHID=0x0111, country=0, flags=0x03 (remote wake + normally connectable)) */
static const uint8_t hid_info[] = { 0x01, 0x01, 0x00, 0x03 };

/* Minimal boot keyboard report map (8-byte input report: modifiers, reserved, 6 keys) */
/* HID Report Map parity with BlueZ (Keyboard Report ID 1, Consumer Report ID 2) */
static const uint8_t report_map[] = {
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01, 0x05, 0x07, 0x19, 0xE0,
    0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01, 0x95, 0x06, 0x75, 0x08, 0x15, 0x00,
    0x25, 0x65, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00, 0xC0, 0x05, 0x0C, 0x09,
    0x01, 0xA1, 0x01, 0x85, 0x02, 0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00,
    0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00, 0xC0,
};
/* --- Battery Service (0x180F) --- */
static uint8_t battery_level = 100; /* Fake 100% */
static bool notify_batt_enabled;


static void batt_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
    ARG_UNUSED(attr);
    notify_batt_enabled = (value == BT_GATT_CCC_NOTIFY);
    LOG_INF("BATT notify %s", notify_batt_enabled ? "ENABLED" : "disabled");
    update_link_ready("batt_ccc");
}

static ssize_t read_battery_level(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                                 void *buf, uint16_t len, uint16_t offset)
{
    ARG_UNUSED(conn);
    const uint8_t *lvl = attr->user_data;
    return bt_gatt_attr_read(conn, attr, buf, len, offset, lvl, sizeof(*lvl));
}

BT_GATT_SERVICE_DEFINE(bas_svc,
    BT_GATT_PRIMARY_SERVICE(BT_UUID_BAS),
    BT_GATT_CHARACTERISTIC(BT_UUID_BAS_BATTERY_LEVEL,
                           BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_READ_ENCRYPT,
                           read_battery_level, NULL, &battery_level),
    BT_GATT_CCC(batt_ccc_changed, BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT)
);


/* Runtime state
 *
 * File-scope state shared across bring-up, BLE callbacks, LED policy, and the
 * SW1 long-press unpair flow. Keeping it in one place makes the control flow
 * easier to follow without changing behavior.
 */
static struct k_work_delayable sw1_feedback_work;
static atomic_t sw1_pressed;
static bool sw1_feedback_on;
static struct bt_conn *current_conn;
static volatile bool adv_is_on;
static volatile bool error_state;
static bool notify_kb_enabled;
static bool notify_cc_enabled;
static bool hid_suspended;
static bool notify_boot_enabled;
static uint8_t current_sec_level;
static bool hid_zero_sent;
static bool link_ready;
static bool have_conn_params;
static uint16_t last_interval;
static uint16_t last_latency;
static uint16_t last_timeout;
static bool have_phy;
static uint8_t last_tx_phy;
static uint8_t last_rx_phy;
static struct k_work_delayable bond_clear_work;
static struct k_timer blink_timer;
static struct k_work_delayable adv_restart_work;
static bool blink_state;

static uint8_t protocol_mode = 1; /* 0=Boot, 1=Report */
/* Reports (Report IDs are indicated via Report Reference descriptors; payloads here do NOT include the ID byte) */
static uint8_t kb_report[8] = { 0 };      /* modifiers, reserved, 6 keys */
static uint8_t cc_report[2] = { 0 };      /* 16-bit Consumer usage */
static uint8_t boot_kb_report[8] = { 0 };

static const struct bt_le_conn_param low_latency_conn_params = {
    .interval_min = 12,
    .interval_max = 12,
    .latency = 0,
    .timeout = 400,
}; /* 7.5-15 ms, latency 0, timeout 4 s */


/* Report Reference descriptor payload: [Report ID, Report Type (Input=1)] */

/* --------------------------------------------------------------------------
 * USB CDC ACM command channel (PiHub <-> dongle)
 *
 * We expose a simple newline-delimited ASCII protocol on CDC ACM 0:
 *   PING            -> PONG
 *   STATUS          -> STATUS adv=<0/1> conn=<0/1> proto=<0/1> err=<0/1>
 *   UNPAIR          -> OK (clears bonds + restarts advertising)
 *   Binary hot path:
 *     0x01 + 8 bytes keyboard report
 *     0x02 + 2 bytes consumer report
 *   No per-frame reply on success.
 *   Rare async errors may be emitted as EVT BINERR ...
 *
 * Telemetry (emitted opportunistically when the link is up):
 *   EVT ADV 0|1
 *   EVT CONN 0|1
 *   EVT PROTO 0|1
 *   EVT CONN_PARAMS interval_ms=<...> latency=<...> timeout_ms=<...>
 *   EVT PHY tx=<1M|2M|Coded> rx=<...>
 *   EVT DISC reason=<n>
 *   EVT ERR 0|1
 * -------------------------------------------------------------------------- */

#if DT_NODE_HAS_STATUS(DT_NODELABEL(cdc_acm_uart0), okay)
#if DT_NODE_HAS_STATUS(DT_ALIAS(pihub_cmd_uart), okay)
#define PIHUB_CMD_UART_NODE DT_ALIAS(pihub_cmd_uart)
#else
#define PIHUB_CMD_UART_NODE DT_NODELABEL(cdc_acm_uart0)
#endif
#define PIHUB_CMD_UART_ENABLED 1
#elif DT_NODE_HAS_STATUS(DT_NODELABEL(zephyr_cdc_acm_uart), okay)
#define PIHUB_CMD_UART_NODE DT_NODELABEL(zephyr_cdc_acm_uart)
#define PIHUB_CMD_UART_ENABLED 1
#else
#define PIHUB_CMD_UART_ENABLED 0
#endif

static const struct device *cmd_uart;
static atomic_t cmd_uart_ready;

/* ---------------- CMD UART non-blocking TX ----------------
 * Best-effort: enqueue and drain via UART TX IRQ.
 * Policy: DROP OLDEST on overflow (better UX than replaying stale bursts).
 */
#define CMD_TX_RB_SIZE 512
RING_BUF_DECLARE(cmd_tx_rb, CMD_TX_RB_SIZE);
static struct k_work_delayable cmd_tx_kick;

static void cmd_uart_kick_tx(void);

static void cmd_tx_kick_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);
    cmd_uart_kick_tx();
}

/* Discard bytes from the ring until there is at least "need" bytes of space. */
static void cmd_tx_drop_oldest_until_space(uint32_t need)
{
    uint32_t space = ring_buf_space_get(&cmd_tx_rb);
    if (space >= need) {
        return;
    }

    uint32_t to_drop = need - space;
    uint8_t tmp[32];

    while (to_drop > 0) {
        uint32_t chunk = MIN(to_drop, (uint32_t)sizeof(tmp));
        uint32_t got = ring_buf_get(&cmd_tx_rb, tmp, chunk);
        if (got == 0) {
            break;
        }
        to_drop -= got;
    }
}

static void cmd_uart_isr(const struct device *dev, void *user_data)
{
    ARG_UNUSED(user_data);

    if (!uart_irq_update(dev)) {
        return;
    }

    while (uart_irq_tx_ready(dev)) {
        uint8_t *data;
        uint32_t len = ring_buf_get_claim(&cmd_tx_rb, &data, 64);

        if (len == 0) {
            uart_irq_tx_disable(dev);
            return;
        }

        int sent = uart_fifo_fill(dev, data, len);
        if (sent <= 0) {
            /* Backend not accepting right now. Stop TX and retry later. */
            uart_irq_tx_disable(dev);
            (void)k_work_schedule(&cmd_tx_kick, K_MSEC(10));
            return;
        }

        ring_buf_get_finish(&cmd_tx_rb, (uint32_t)sent);
    }
}

static void cmd_uart_kick_tx(void)
{
    if (!atomic_get(&cmd_uart_ready) || (cmd_uart == NULL)) {
        return;
    }

    if (ring_buf_size_get(&cmd_tx_rb) > 0) {
        uart_irq_tx_enable(cmd_uart);
    }
}

static void cmd_uart_send_str(const char *s)
{
    if (!atomic_get(&cmd_uart_ready) || (cmd_uart == NULL)) {
        return;
    }

    size_t len = strlen(s);
    if (len == 0) {
        return;
    }

    cmd_tx_drop_oldest_until_space((uint32_t)len);
    (void)ring_buf_put(&cmd_tx_rb, (const uint8_t *)s, (uint32_t)len);

    cmd_uart_kick_tx();
}

static void cmd_uart_send_line(const char *s)
{
    cmd_uart_send_str(s);
    cmd_uart_send_str("\n");
}

static void cmd_evt_adv(bool on)      { adv_is_on = on; char b[24]; snprintk(b, sizeof(b), "EVT ADV %d", on ? 1 : 0); cmd_uart_send_line(b); }
static void cmd_evt_conn(bool on)     { char b[24]; snprintk(b, sizeof(b), "EVT CONN %d", on ? 1 : 0); cmd_uart_send_line(b); }
static void cmd_evt_ready(bool on)    { char b[32]; snprintk(b, sizeof(b), "EVT READY %d", on ? 1 : 0); cmd_uart_send_line(b); }
static void cmd_evt_proto(uint8_t pm) { char b[24]; snprintk(b, sizeof(b), "EVT PROTO %u", (unsigned int)(pm ? 1 : 0)); cmd_uart_send_line(b); }
static void cmd_evt_err(bool on)
{
    error_state = on;

    if (on) {
        /* Error state wins over normal adv/conn indication. */
        k_timer_stop(&blink_timer);
        pihub_led_err_on();
    } else if (!atomic_get(&sw1_pressed)) {
        /* Restore normal LED state when error clears, unless SW1 feedback is active. */
        if (current_conn) {
            set_leds_conn();
        } else {
            blink_state = false;
            set_leds_adv();
            k_timer_start(&blink_timer, K_NO_WAIT, K_MSEC(500));
        }
    }

    char b[24];
    snprintk(b, sizeof(b), "EVT ERR %d", on ? 1 : 0);
    cmd_uart_send_line(b);
}

/* Local forward decls */
static void hid_release_all_best_effort(void);

static void cmd_evt_disc(uint8_t reason)
{
    char b[32];
    snprintk(b, sizeof(b), "EVT DISC reason=%u", reason);
    cmd_uart_send_line(b);
}

static void cmd_evt_conn_params(uint16_t interval, uint16_t latency, uint16_t timeout)
{
    have_conn_params = true;
    last_interval = interval;
    last_latency  = latency;
    last_timeout  = timeout; /* 10ms units */

    char b[96];
    uint32_t interval_ms = ((uint32_t)interval * 125U) / 100U; /* 1.25ms units -> whole ms */
    uint32_t timeout_ms = (uint32_t)timeout * 10U;             /* 10ms units -> ms */

    snprintk(b, sizeof(b), "EVT CONN_PARAMS interval_ms=%u latency=%u timeout_ms=%u",
             (unsigned)interval_ms, (unsigned)latency, (unsigned)timeout_ms);
    cmd_uart_send_line(b);
}


static void cmd_evt_phy(uint8_t tx_phy, uint8_t rx_phy)
{
    have_phy = true;
    last_tx_phy = tx_phy;
    last_rx_phy = rx_phy;
    char b[64];
    snprintk(b, sizeof(b), "EVT PHY tx=%s rx=%s", phy_to_str(tx_phy), phy_to_str(rx_phy));
    cmd_uart_send_line(b);
}

static bool compute_link_ready(void)
{
    bool kb_path_ready = notify_kb_enabled || notify_boot_enabled;
    bool cc_path_ready = notify_cc_enabled;

    return (current_conn != NULL) &&
           (current_sec_level >= BT_SECURITY_L2) &&
           !hid_suspended &&
           kb_path_ready &&
           cc_path_ready;
}


static void cmd_emit_status_snapshot(void)
{
    /* Recompute readiness on demand so a PiHub restart can resync state even
     * if READY transitioned earlier (or if notifications were enabled after
     * connect/security).
     */
    bool now_ready = compute_link_ready();

    /* Keep the cached flag in sync for future change detection. */
    link_ready = now_ready;

    cmd_evt_ready(now_ready);
    cmd_evt_adv(adv_is_on);
    cmd_evt_conn(current_conn != NULL);
    cmd_evt_proto(protocol_mode);
    cmd_evt_err(error_state ? 1 : 0);

    if (have_conn_params) {
        cmd_evt_conn_params(last_interval, last_latency, last_timeout);
    }

    if (have_phy) {
        cmd_evt_phy(last_tx_phy, last_rx_phy);
    }
}


static void cmd_emit_status_line(void)
{
    char b[340];

    uint32_t interval_ms = 0;
    uint32_t timeout_ms = 0;

    if (have_conn_params) {
        interval_ms = ((uint32_t)last_interval * 125U) / 100U; /* 1.25ms units -> whole ms */
        timeout_ms = (uint32_t)last_timeout * 10U;             /* 10ms units -> ms */
    }

    snprintk(b, sizeof(b),
             "STATUS adv=%d conn=%d sec=%u ready=%d proto=%u err=%d kb_notify=%d boot_notify=%d cc_notify=%d batt_notify=%d "
             "suspend=%d interval_ms=%u latency=%u timeout_ms=%u phy_tx=%u phy_rx=%u",
             adv_is_on ? 1 : 0,
             current_conn ? 1 : 0,
             (unsigned)current_sec_level,
             link_ready ? 1 : 0,
             (unsigned)protocol_mode,
             error_state ? 1 : 0,
             notify_kb_enabled ? 1 : 0,
             notify_boot_enabled ? 1 : 0,
             notify_cc_enabled ? 1 : 0,
             notify_batt_enabled ? 1 : 0,
             hid_suspended ? 1 : 0,
             (unsigned)interval_ms,
             (unsigned)last_latency,
             (unsigned)timeout_ms,
             (unsigned)last_tx_phy,
             (unsigned)last_rx_phy);

    cmd_uart_send_line(b);
}


static void update_link_ready(const char *why)
{
    bool now = compute_link_ready();

    if (now != link_ready) {
        link_ready = now;
        cmd_evt_ready(now);
        LOG_INF("READY %d (%s)", now ? 1 : 0, why ? why : "");
    }
}



static const char *phy_to_str(uint8_t phy)
{
    switch (phy) {
    case BT_GAP_LE_PHY_1M: return "1M";
    case BT_GAP_LE_PHY_2M: return "2M";
    case BT_GAP_LE_PHY_CODED: return "Coded";
    default: return "?";
    }
}

static bool hex_nibble(char c, uint8_t *out)
{
    if ((c >= '0') && (c <= '9')) { *out = (uint8_t)(c - '0'); return true; }
    c = (char)tolower((unsigned char)c);
    if ((c >= 'a') && (c <= 'f')) { *out = (uint8_t)(10 + (c - 'a')); return true; }
    return false;
}

static bool hex_to_bytes(const char *hex, uint8_t *out, size_t out_len)
{
    /* Expect exactly out_len*2 hex chars, no separators. */
    for (size_t i = 0; i < out_len; i++) {
        uint8_t hi, lo;
        if (!hex_nibble(hex[i * 2], &hi) || !hex_nibble(hex[i * 2 + 1], &lo)) {
            return false;
        }
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return true;
}

static void cmd_handle_line(char *line);

/* --- BIN hot-path opcodes (PiHub -> dongle) ---
 * 0x01: keyboard report payload (8 bytes)
 * 0x02: consumer report payload (2 bytes, little-endian usage)
 *
 * Everything else remains ASCII newline-delimited (PING/STATUS/UNPAIR).
 */
#define PIHUB_BIN_KB 0x01
#define PIHUB_BIN_CC 0x02

#define CMD_BIN_READ_TIMEOUT_MS 30
#define CMD_ASCII_MAX_LEN       95

static bool cmd_uart_read_exact(uint8_t *dst, size_t n, uint32_t timeout_ms)
{
    int64_t deadline = k_uptime_get() + (int64_t)timeout_ms;

    for (size_t i = 0; i < n; i++) {
        while (uart_poll_in(cmd_uart, &dst[i]) != 0) {
            if (k_uptime_get() >= deadline) {
                return false;
            }
            k_msleep(1);
        }
    }

    return true;
}


static bool cmd_handle_bin_kb(void)
{
    uint8_t report8[8];

    if (!cmd_uart_read_exact(report8, sizeof(report8), CMD_BIN_READ_TIMEOUT_MS)) {
        cmd_evt_err(true);
        cmd_uart_send_line("EVT BINERR kb timeout");
        return false;
    }

    int rc = send_keyboard_report(report8);

    /* Parser/transport recovered enough to parse a full frame again. */
    if (error_state) {
        cmd_evt_err(false);
    }

    /* Hot path is fire-and-forget: no per-frame ASCII ACKs. */
    if (rc != 0 && rc != -EBUSY && rc != -EAGAIN && rc != -ENOMEM) {
        char b[32];
        snprintk(b, sizeof(b), "EVT BINERR kb %d", rc);
        cmd_uart_send_line(b);
    }

    return true;
}



static bool cmd_handle_bin_cc(void)
{
    uint8_t report2[2];

    if (!cmd_uart_read_exact(report2, sizeof(report2), CMD_BIN_READ_TIMEOUT_MS)) {
        cmd_evt_err(true);
        cmd_uart_send_line("EVT BINERR cc timeout");
        return false;
    }

    int rc = send_consumer_report(report2);

    /* Parser/transport recovered enough to parse a full frame again. */
    if (error_state) {
        cmd_evt_err(false);
    }

    /* Hot path is fire-and-forget: no per-frame ASCII ACKs. */
    if (rc != 0 && rc != -EBUSY && rc != -EAGAIN && rc != -ENOMEM) {
        char b[32];
        snprintk(b, sizeof(b), "EVT BINERR cc %d", rc);
        cmd_uart_send_line(b);
    }

    return true;
}



/* Command RX thread */
#define CMD_RX_STACK 1536
#define CMD_RX_PRIO  5
K_THREAD_STACK_DEFINE(cmd_rx_stack, CMD_RX_STACK);
static struct k_thread cmd_rx_thread;

static void cmd_rx_resync_to_newline(void)
{
    int64_t deadline = k_uptime_get() + 50; /* short flush window */
    uint8_t ch;

    while (k_uptime_get() < deadline) {
        if (uart_poll_in(cmd_uart, &ch) != 0) {
            k_msleep(1);
            continue;
        }

        if (ch == '\n') {
            break;
        }
    }
}


static void cmd_rx_thread_fn(void *a, void *b, void *c)
{
    ARG_UNUSED(a);
    ARG_UNUSED(b);
    ARG_UNUSED(c);

    if (cmd_uart == NULL) {
        return;
    }

    /* Wait for host to open the port (DTR). We do NOT talk until DTR=1, so macOS won't
     * create "ghost" ttys that appear dead. PiHub should open the port which asserts DTR.
     */
    uint32_t dtr = 0;
    while (1) {
        (void)uart_line_ctrl_get(cmd_uart, UART_LINE_CTRL_DTR, &dtr);
        if (dtr) {
            break;
        }
        k_msleep(50);
    }

    atomic_set(&cmd_uart_ready, 1);

    /* Identify this port for PiHub's auto-detect. */
    cmd_uart_send_line("EVT PORT CMD");
    cmd_uart_send_line("EVT USB 1");
    cmd_uart_send_line("EVT BOOT pihub-hid");

    /* ASCII fallback buffer (for PING/STATUS/UNPAIR and any debug commands). */
    char line[96];
    size_t n = 0;

    while (1) {
        uint8_t ch;
        int rc = uart_poll_in(cmd_uart, &ch);
        if (rc != 0) {
            k_msleep(2);
            continue;
        }

        /* BIN hot-path: a single byte opcode + fixed payload. */
        if (ch == PIHUB_BIN_KB) {
            bool ok = cmd_handle_bin_kb();
            if (!ok) {
                n = 0;
                cmd_rx_resync_to_newline();
            }
            continue;
        }

        if (ch == PIHUB_BIN_CC) {
            bool ok = cmd_handle_bin_cc();
            if (!ok) {
                n = 0;
                cmd_rx_resync_to_newline();
            }
            continue;
        }

        /* ASCII path (newline-delimited). */
        if (ch == '\r') {
            continue;
        }

        if (ch == '\n') {
            line[n] = '\0';
            if (n > 0) {
                cmd_handle_line(line);
            }
            n = 0;
            continue;
        }

        if (n < (sizeof(line) - 1)) {
            line[n++] = (char)ch;
        } else {
            /* Overflow: report and drop until next newline. */
            cmd_uart_send_line("ERR -E2BIG");
            n = 0;
            cmd_rx_resync_to_newline();
        }
    }
}


static void cmd_uart_init(void)
{
#if PIHUB_CMD_UART_ENABLED
    cmd_uart = DEVICE_DT_GET(PIHUB_CMD_UART_NODE);
    if (!device_is_ready(cmd_uart)) {
        LOG_WRN("CMD UART not ready (DT_ALIAS(pihub_cmd_uart)). No PiHub protocol I/O.");
        cmd_uart = NULL;
        return;
    }

    /* Optional: make sure line control is enabled (ignored on some backends). */
    (void)uart_line_ctrl_set(cmd_uart, UART_LINE_CTRL_DCD, 1);
    (void)uart_line_ctrl_set(cmd_uart, UART_LINE_CTRL_DSR, 1);

    /* Non-blocking TX init */
    k_work_init_delayable(&cmd_tx_kick, cmd_tx_kick_work_handler);
    (void)uart_irq_callback_set(cmd_uart, cmd_uart_isr);
    uart_irq_tx_disable(cmd_uart);

    atomic_set(&cmd_uart_ready, 0);

    k_thread_create(&cmd_rx_thread, cmd_rx_stack,
                    K_THREAD_STACK_SIZEOF(cmd_rx_stack),
                    cmd_rx_thread_fn, NULL, NULL, NULL,
                    CMD_RX_PRIO, 0, K_NO_WAIT);
    k_thread_name_set(&cmd_rx_thread, "cmd_rx");
#else
    cmd_uart = NULL;
    atomic_set(&cmd_uart_ready, 0);
#endif
}


/* SW1 long-press unpair flow
 *
 * Holding SW1 schedules a delayed bond-clear action. Releasing early cancels it.
 * A small LED feedback worker runs while the button is held, and normal LED state
 * is restored from the actual link state when the hold ends.
 */
/* Prefer direct GPIO for SW1: more reliable than dk_buttons on nrf52840dongle. */
#define SW1_NODE DT_ALIAS(sw0)
#if DT_NODE_HAS_STATUS(SW1_NODE, okay)
static const struct gpio_dt_spec sw1 = GPIO_DT_SPEC_GET(SW1_NODE, gpios);
static struct gpio_callback sw1_cb;

/* --------------------------------------------------------------------------
 * Settings / storage helpers
 *
 * Bonds live in the settings backend. These helpers keep the erase path explicit
 * so SW1 long-press and UNPAIR can clear pairing state without needing a probe.
 */

static int pihub_open_storage(const struct flash_area **fa_out)
{
    const struct flash_area *fa = NULL;
    int err = flash_area_open(FLASH_AREA_ID(storage), &fa);
    if (err) {
        return err;
    }

    *fa_out = fa;
    return 0;
}

static void pihub_close_storage(const struct flash_area *fa)
{
    if (fa != NULL) {
        flash_area_close(fa);
    }
}

static int pihub_erase_settings_storage(void)
{
    const struct flash_area *fa = NULL;
    int err = pihub_open_storage(&fa);

    if (err) {
        LOG_ERR("flash_area_open(storage) failed: %d", err);
        return err;
    }

    err = flash_area_erase(fa, 0, fa->fa_size);
    if (err) {
        LOG_ERR("flash_area_erase(storage) failed: %d", err);
    } else {
        LOG_INF("Settings storage erased (%u bytes)", (unsigned int)fa->fa_size);
    }

    pihub_close_storage(fa);
    return err;
}

static int pihub_settings_init_only(void)
{
    int err = settings_subsys_init();
    if (err) {
        LOG_ERR("settings_subsys_init failed (err %d)", err);
        return err;
    }

    LOG_INF("Settings subsystem init OK");
    return 0;
}

/* Must be called *after* bt_enable() so bonded keys can be applied to BT. */
static int pihub_settings_load_after_bt(void)
{
    int err = settings_load();
    if (err) {
        LOG_ERR("settings_load failed (err %d)", err);
        return err;
    }
    LOG_INF("Settings loaded");
    return 0;
}
#endif

/* LED policy
 *
 * All user-visible LED behavior should go through this section. Higher-level
 * state paths should call semantic helpers rather than touching DK_LEDx directly.
 */
static void pihub_leds_all_off(void)
{
    dk_set_led_off(DK_LED1);
    dk_set_led_off(DK_LED2);
    dk_set_led_off(DK_LED3);
    dk_set_led_off(DK_LED4);
}

static void pihub_led_err_on(void)
{
    pihub_leds_all_off();
    dk_set_led_on(PIHUB_LED_ERR);
}

static void pihub_led_conn_on(void)
{
    pihub_leds_all_off();
    dk_set_led_on(PIHUB_LED_CONN);
}

static void pihub_led_adv_set(bool on)
{
    /* Keep adv as blue-only blink */
    dk_set_led_off(PIHUB_LED_ERR);
    dk_set_led_off(PIHUB_LED_CONN);
    dk_set_led(PIHUB_LED_ADV, on ? 1 : 0);
}

static void pihub_led_restore_state(void)
{
    if (current_conn) {
        set_leds_conn();
    } else {
        set_leds_adv();
    }
}

static void sw1_feedback_work_fn(struct k_work *work)
{
    ARG_UNUSED(work);

    if (!atomic_get(&sw1_pressed)) {
        sw1_feedback_on = false;
        pihub_led_restore_state();
        return;
    }

    sw1_feedback_on = !sw1_feedback_on;

    if (sw1_feedback_on) {
        pihub_leds_all_off();
        dk_set_led_on(PIHUB_LED_ERR);
    } else {
        pihub_led_restore_state();
    }

    k_work_schedule(&sw1_feedback_work, K_MSEC(200));
}

/* Test/bring-up sequence tuning.
 * We keep this conservative to improve iOS/tvOS reliability on (re)connect:
 * - Wait for encryption + CCCs to settle
 * - Send an "all keys up" (empty) report before any key down
 * - Stagger keyboard and consumer reports
 * - Retry on temporary notify back-pressure (-ENOMEM/-EAGAIN/-EBUSY)
 */
#define TEST_PREROLL_DELAY_MS 60
#define TEST_KEY_HOLD_MS      35
#define TEST_CC_RELEASE_MS    120




static ssize_t read_hid_info(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                             void *buf, uint16_t len, uint16_t offset)
{
    return bt_gatt_attr_read(conn, attr, buf, len, offset, hid_info, sizeof(hid_info));
}

static ssize_t read_report_map(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                               void *buf, uint16_t len, uint16_t offset)
{
    return bt_gatt_attr_read(conn, attr, buf, len, offset, report_map, sizeof(report_map));
}

static ssize_t read_protocol_mode(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                                  void *buf, uint16_t len, uint16_t offset)
{
    return bt_gatt_attr_read(conn, attr, buf, len, offset, &protocol_mode, sizeof(protocol_mode));
}

static ssize_t write_protocol_mode(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                                   const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
    ARG_UNUSED(conn);
    ARG_UNUSED(attr);
    ARG_UNUSED(flags);

    if (offset != 0 || len != 1) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    }

    uint8_t new_mode = ((const uint8_t *)buf)[0] ? 1 : 0;

    if (new_mode != protocol_mode) {
        protocol_mode = new_mode;

        LOG_INF("Protocol Mode set: 0x%02x (%s)",
                protocol_mode,
                protocol_mode ? "Report" : "Boot");

        cmd_evt_proto(protocol_mode);

        /* Keyboard path changes with protocol mode, so READY may change too. */
        update_link_ready("proto_mode");
    } else {
        protocol_mode = new_mode;
        LOG_INF("Protocol Mode unchanged: 0x%02x (%s)",
                protocol_mode,
                protocol_mode ? "Report" : "Boot");
    }

    return len;
}


static void kb_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
    ARG_UNUSED(attr);
    notify_kb_enabled = (value == BT_GATT_CCC_NOTIFY);
    LOG_INF("KB notify %s", notify_kb_enabled ? "ENABLED" : "disabled");

    if (notify_kb_enabled) {
        hid_zero_sent = false;

        /* Give Apple hosts a moment after enabling CCCD before the first input report. */
    }
    update_link_ready("kb_ccc");
}

static void cc_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
    ARG_UNUSED(attr);
    notify_cc_enabled = (value == BT_GATT_CCC_NOTIFY);
    LOG_INF("CC notify %s", notify_cc_enabled ? "ENABLED" : "disabled");
    update_link_ready("cc_ccc");
}
static void boot_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
    ARG_UNUSED(attr);
    notify_boot_enabled = (value == BT_GATT_CCC_NOTIFY);
    LOG_INF("BOOT notify %s", notify_boot_enabled ? "ENABLED" : "disabled");

    if (notify_boot_enabled) {
        hid_zero_sent = false;

        /* Give Apple hosts a moment after enabling CCCD before the first input report. */
    }
    update_link_ready("boot_ccc");
}

static ssize_t read_kb_report(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                          void *buf, uint16_t len, uint16_t offset)
{
    return bt_gatt_attr_read(conn, attr, buf, len, offset, kb_report, sizeof(kb_report));
}

static ssize_t read_cc_report(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                          void *buf, uint16_t len, uint16_t offset)
{
    return bt_gatt_attr_read(conn, attr, buf, len, offset, cc_report, sizeof(cc_report));
}

static ssize_t read_boot_kb_report(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                          void *buf, uint16_t len, uint16_t offset)
{
    return bt_gatt_attr_read(conn, attr, buf, len, offset, boot_kb_report, sizeof(boot_kb_report));
}


/* Report Reference descriptor: [Report ID, Report Type (Input=1)] */
static const uint8_t kb_report_ref[] = { 0x01, 0x01 }; /* Report ID 1, Input */
static const uint8_t cc_report_ref[] = { 0x02, 0x01 }; /* Report ID 2, Input */

static ssize_t read_input_report_ref(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                                     void *buf, uint16_t len, uint16_t offset)
{
    /* Use descriptor's user_data as the backing store (must be a constant buffer) */
    return bt_gatt_attr_read(conn, attr, buf, len, offset,
                             attr->user_data, 2);
}


/* HID Control Point (0x2A4C) write handler.
 * Hosts may use this to signal Suspend (0x01) or Exit Suspend (0x00).
 * We track the suspend bit so READY/STATUS reflect whether the input path is
 * currently expected to be active.
 */
static ssize_t write_ctrl_point(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                                const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
    ARG_UNUSED(conn);
    ARG_UNUSED(attr);
    ARG_UNUSED(offset);
    ARG_UNUSED(flags);

    if (len < 1U) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    }

    const uint8_t *cp = (const uint8_t *)buf;

    switch (cp[0]) {
    case 0x00:
        hid_suspended = false;
        break;
    case 0x01:
        hid_suspended = true;
        break;
    default:
        break;
    }

    update_link_ready("ctrl_point");
    LOG_INF("HID Control Point write: 0x%02x", cp[0]);
    return len;
}

/* HID Service (0x1812) */
BT_GATT_SERVICE_DEFINE(hids_svc,
    BT_GATT_PRIMARY_SERVICE(BT_UUID_HIDS),

    /* HID Information */
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_INFO,
                           BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ_ENCRYPT,
                           read_hid_info, NULL, NULL),

    /* HID Report Map */
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_REPORT_MAP,
                           BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ_ENCRYPT,
                           read_report_map, NULL, NULL),

    /* Protocol Mode */
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_PROTOCOL_MODE,
                           BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
                           BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT,
                           read_protocol_mode, write_protocol_mode, NULL),

    /* Report (Keyboard, Report ID 1) */
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_REPORT,
                           BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_READ_ENCRYPT,
                           read_kb_report, NULL, NULL),
    BT_GATT_CCC(kb_ccc_changed, BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT),
    BT_GATT_DESCRIPTOR(BT_UUID_HIDS_REPORT_REF,
                       BT_GATT_PERM_READ_ENCRYPT,
                       read_input_report_ref, NULL, (void *)kb_report_ref),

    /* Report (Consumer Control, Report ID 2) */
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_REPORT,
                           BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_READ_ENCRYPT,
                           read_cc_report, NULL, NULL),
    BT_GATT_CCC(cc_ccc_changed, BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT),
    BT_GATT_DESCRIPTOR(BT_UUID_HIDS_REPORT_REF,
                       BT_GATT_PERM_READ_ENCRYPT,
                       read_input_report_ref, NULL, (void *)cc_report_ref),

    /* Boot Keyboard Input Report (no Report ID byte) */
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_BOOT_KB_IN_REPORT,
                           BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_READ_ENCRYPT,
                           read_boot_kb_report, NULL, NULL),
    BT_GATT_CCC(boot_ccc_changed, BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT),

    /* HID Control Point */
    BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_CTRL_POINT,
                           BT_GATT_CHRC_WRITE_WITHOUT_RESP,
                           BT_GATT_PERM_WRITE_ENCRYPT,
                           NULL, write_ctrl_point, NULL)
);

/* Attribute indices for notifications (keep in sync with hids_svc definition above) */
enum hids_attr_index {
    HIDS_ATTR_PRIMARY = 0,
    HIDS_ATTR_INFO_CHRC, HIDS_ATTR_INFO_VAL,
    HIDS_ATTR_MAP_CHRC, HIDS_ATTR_MAP_VAL,
    HIDS_ATTR_PROTO_CHRC, HIDS_ATTR_PROTO_VAL,
    HIDS_ATTR_KB_CHRC, HIDS_ATTR_KB_VAL, HIDS_ATTR_KB_CCC, HIDS_ATTR_KB_REF,
    HIDS_ATTR_CC_CHRC, HIDS_ATTR_CC_VAL, HIDS_ATTR_CC_CCC, HIDS_ATTR_CC_REF,
    HIDS_ATTR_BOOT_CHRC, HIDS_ATTR_BOOT_VAL, HIDS_ATTR_BOOT_CCC,
    HIDS_ATTR_CTRL_CHRC, HIDS_ATTR_CTRL_VAL,
};


/* Device Information Service strings. */
static const char mfg_name[] = "PiHub";
static const char model_num[] = DEVICE_NAME;
static const char hw_rev[]    = "revA";
static const char fw_rev[]    = "1.0.0";
static const char sw_rev[]    = "1.0.0";

static char serial_num[32] = "UNKNOWN";

static void init_serial_num(void)
{
    uint8_t id[8];
    ssize_t n = hwinfo_get_device_id(id, sizeof(id));

    if (n <= 0) {
        return;
    }

    size_t off = 0;
    for (ssize_t i = 0; i < n && off + 2 < sizeof(serial_num); i++) {
        off += snprintk(&serial_num[off], sizeof(serial_num) - off, "%02X", id[i]);
    }
}

static ssize_t read_str(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                        void *buf, uint16_t len, uint16_t offset)
{
    const char *s = attr->user_data;
    return bt_gatt_attr_read(conn, attr, buf, len, offset, s, (uint16_t)strlen(s));
}

BT_GATT_SERVICE_DEFINE(dis_svc,
    BT_GATT_PRIMARY_SERVICE(BT_UUID_DIS),

    BT_GATT_CHARACTERISTIC(BT_UUID_DIS_MANUFACTURER_NAME,
                           BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ,
                           read_str, NULL, (void *)mfg_name),

    BT_GATT_CHARACTERISTIC(BT_UUID_DIS_MODEL_NUMBER,
                           BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ,
                           read_str, NULL, (void *)model_num),

    BT_GATT_CHARACTERISTIC(BT_UUID_DIS_HARDWARE_REVISION,
                           BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ,
                           read_str, NULL, (void *)hw_rev),

    BT_GATT_CHARACTERISTIC(BT_UUID_DIS_FIRMWARE_REVISION,
                           BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ,
                           read_str, NULL, (void *)fw_rev),

    BT_GATT_CHARACTERISTIC(BT_UUID_DIS_SOFTWARE_REVISION,
                           BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ,
                           read_str, NULL, (void *)sw_rev),

    /* Unique serial: keep encrypted */
    BT_GATT_CHARACTERISTIC(BT_UUID_DIS_SERIAL_NUMBER,
                           BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ_ENCRYPT,
                           read_str, NULL, (void *)serial_num)
);

/* Advertising data: Flags + HID service UUID (16-bit) */
static const struct bt_data ad[] = {
    BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
    /* Advertise as HID (Generic) and Battery in the 16-bit UUID list */
    BT_DATA_BYTES(BT_DATA_UUID16_ALL,
                  0x12, 0x18, /* 0x1812 HID */
                  0x0F, 0x18  /* 0x180F Battery */
                  ),
    /* GAP Appearance AD type (0x19): 0x03C0 = Generic HID */
    BT_DATA_BYTES(BT_DATA_GAP_APPEARANCE, 0xC0, 0x03),
};

/* Put the configured device name in the scan response. */
static const struct bt_data sd[] = {
    BT_DATA(BT_DATA_NAME_COMPLETE, DEVICE_NAME, sizeof(DEVICE_NAME) - 1),
};


static void adv_restart_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);

    /* Idempotent restart: stop first (ignore errors), then start. */
    (void)bt_le_adv_stop();

    int err = bt_le_adv_start(PIHUB_ADV_PARAM, ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));
    if (err) {
        /* Many start failures are transient; keep trying rather than going “dead”. */
        LOG_WRN("Adv start failed (err %d) - retrying", err);
        k_work_schedule(&adv_restart_work, K_SECONDS(1));
        return;
    }

    LOG_INF("Advertising started");
        cmd_evt_adv(true);
}

/*
 * SW1 long-press: clear bonds + restart advertising.
 * This is the "fix it" button after you "Forget" on iPhone.
 */
static void bond_clear_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);

    LOG_WRN("SW1 held 5s: clearing bonds + wiping settings storage");
    printk("[PiHub] SW1 5s: clear bonds + wipe settings\n");
    
    /* Best-effort: release all keys before we tear anything down. */
    hid_release_all_best_effort();

    /* If connected, drop the link first (ignore errors). */
    if (current_conn) {
        (void)bt_conn_disconnect(current_conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
    }

    /* Clear all bonds for our default identity. */
    (void)bt_unpair(BT_ID_DEFAULT, BT_ADDR_LE_ANY);

    /* Also wipe the settings/NVS partition so we start from a known-good state.
     * This is the only reliable "erase" when you're flashing over USB DFU.
     */
    int err = pihub_erase_settings_storage();
    if (err) {
        LOG_ERR("Settings storage erase failed: %d", err);
        printk("[PiHub] settings storage erase failed: %d\n", err);
    } else {
        LOG_WRN("Settings storage erased; rebooting");
        printk("[PiHub] settings storage erased; rebooting\n");
        k_msleep(150);
        sys_reboot(SYS_REBOOT_COLD);
    }

    /* If we couldn't erase, at least try to resume advertising. */
    set_leds_adv();
    k_timer_start(&blink_timer, K_NO_WAIT, K_MSEC(500));
    k_work_schedule(&adv_restart_work, K_MSEC(100));
}

static void sw1_schedule_or_cancel(bool pressed)
{
    if (pressed) {
        atomic_set(&sw1_pressed, 1);
        sw1_feedback_on = false;
        k_work_schedule(&bond_clear_work, K_SECONDS(5));
        k_work_schedule(&sw1_feedback_work, K_NO_WAIT);
    } else {
        atomic_set(&sw1_pressed, 0);
        (void)k_work_cancel_delayable(&bond_clear_work);
        (void)k_work_cancel_delayable(&sw1_feedback_work);
        sw1_feedback_on = false;
        pihub_led_restore_state();
    }
}

#if DT_NODE_HAS_STATUS(SW1_NODE, okay)
static void sw1_gpio_isr(const struct device *dev, struct gpio_callback *cb, uint32_t pins)
{
    ARG_UNUSED(dev);
    ARG_UNUSED(cb);
    ARG_UNUSED(pins);

    int val = gpio_pin_get_dt(&sw1);
    bool pressed = (val > 0) ? false : true; /* button is usually active-low */

    if (pressed) {
        if (atomic_cas(&sw1_pressed, 0, 1)) {
            sw1_schedule_or_cancel(true);
        }
    } else {
        if (atomic_cas(&sw1_pressed, 1, 0)) {
            sw1_schedule_or_cancel(false);
        }
    }
}
#endif

static void button_changed(uint32_t button_state, uint32_t has_changed)
{
    /* DK-library fallback (if enabled/working). SW1 maps to DK_BTN1_MSK. */
    if (has_changed & DK_BTN1_MSK) {
        sw1_schedule_or_cancel((button_state & DK_BTN1_MSK) != 0);
    }
}



static int send_keyboard_report(const uint8_t report8[8])
{
    if (!current_conn) {
        return -ENOTCONN;
    }
    if (current_sec_level < BT_SECURITY_L2) {
        return -EACCES;
    }

    /* Match BlueZ behavior: route based on Protocol Mode.
     *  - protocol_mode == 0x00: Boot protocol -> notify Boot Keyboard Input Report (2A22)
     *  - protocol_mode == 0x01: Report protocol -> notify Keyboard Report characteristic (2A4D + Report Ref ID 1)
     */
    if (protocol_mode == 0) {
        if (!notify_boot_enabled) {
            return -EACCES;
        }
        memcpy(boot_kb_report, report8, sizeof(boot_kb_report));
        return bt_gatt_notify(current_conn, &hids_svc.attrs[HIDS_ATTR_BOOT_VAL],
                              boot_kb_report, sizeof(boot_kb_report));
    }

    if (!notify_kb_enabled) {
        return -EACCES;
    }
    memcpy(kb_report, report8, sizeof(kb_report));
    return bt_gatt_notify(current_conn, &hids_svc.attrs[HIDS_ATTR_KB_VAL],
                          kb_report, sizeof(kb_report));
}

static __attribute__((unused)) int send_consumer_report(const uint8_t report2[2])
{
    if (!current_conn) {
        return -ENOTCONN;
    }
    if (current_sec_level < BT_SECURITY_L2) {
        return -EACCES;
    }
    if (!notify_cc_enabled) {
        return -EACCES;
    }

    /* Consumer Control report is 2 bytes (16-bit usage, little-endian). */
    cc_report[0] = report2[0];
    cc_report[1] = report2[1];

    return bt_gatt_notify(current_conn, &hids_svc.attrs[HIDS_ATTR_CC_VAL],
                          cc_report, sizeof(cc_report));
}


static void hid_release_all_best_effort(void)
{
    if (!current_conn) {
        return;
    }

    uint8_t zeros8[8] = { 0 };
    uint8_t zeros2[2] = { 0 };

    int kb_rc1 = send_keyboard_report(zeros8);
    int cc_rc1 = send_consumer_report(zeros2);

    /* Tiny gap to let notify path drain. */
    k_msleep(10);

    int kb_rc2 = send_keyboard_report(zeros8);
    int cc_rc2 = send_consumer_report(zeros2);

    /* Best-effort only: just log if both attempts failed for a path. */
    if (kb_rc1 != 0 && kb_rc2 != 0) {
        LOG_WRN("release-all keyboard failed twice (proto=%u rc1=%d rc2=%d)",
                (unsigned)protocol_mode, kb_rc1, kb_rc2);
    }

    if (cc_rc1 != 0 && cc_rc2 != 0) {
        LOG_WRN("release-all consumer failed twice (rc1=%d rc2=%d)",
                cc_rc1, cc_rc2);
    }
}


static void cmd_handle_line(char *line)
{
    /* Trim leading whitespace */
    while (*line == ' ' || *line == '\t') {
        line++;
    }

    /* Trim trailing CR/LF/space/tab */
    size_t n = strlen(line);
    while (n > 0 && (line[n - 1] == '\r' || line[n - 1] == '\n' || line[n - 1] == ' ' || line[n - 1] == '\t')) {
        line[--n] = '\0';
    }
    if (n == 0) {
        return;
    }

    /* Split command + args (command is case-insensitive). */
    char *args = NULL;
    for (char *c = line; *c; c++) {
        if (*c == ' ' || *c == '\t') {
            *c = '\0';
            args = c + 1;
            break;
        }
    }
    if (args) {
        while (*args == ' ' || *args == '\t') {
            args++;
        }
        if (*args == '\0') {
            args = NULL;
        }
    }

    /* Uppercase command token in-place. */
    for (char *c = line; *c; c++) {
        if (*c >= 'a' && *c <= 'z') {
            *c = (char)(*c - 'a' + 'A');
        }
    }

    if (!strcmp(line, "PING")) {
        cmd_uart_send_line("PONG");
        return;
    }

    if (!strcmp(line, "STATUS") || !strcmp(line, "SNAP")) {
        /* SNAP is an alias for STATUS (single-line poll). */
        cmd_emit_status_line();
        return;
    }

    if (!strcmp(line, "SNAPEVT") || !strcmp(line, "STATUS_EVT")) {
        /* Verbose multi-line snapshot (events). */
        cmd_emit_status_snapshot();
        return;
    }

    if (!strcmp(line, "INFO")) {
        char b[220];
        bt_addr_le_t addrs[1];
        size_t count = 1;

        bt_id_get(addrs, &count);
        if (count == 0) {
            snprintk(b, sizeof(b), "INFO name=%s fw=%s addr=?", DEVICE_NAME, FW_VERSION_STR);
        } else {
            char addr_str[BT_ADDR_LE_STR_LEN];
            bt_addr_le_to_str(&addrs[0], addr_str, sizeof(addr_str));
            snprintk(b, sizeof(b), "INFO name=%s fw=%s addr=%s", DEVICE_NAME, FW_VERSION_STR, addr_str);
        }
        cmd_uart_send_line(b);
        return;
    }

    if (!strcmp(line, "UNPAIR")) {
        /* Parity with SW1 long-press:
         * - release all keys (best effort)
         * - clear bonds + wipe settings storage
         * - reboot (inside the work handler)
         */
        hid_release_all_best_effort();

        /* Schedule the exact same path as the button */
        k_work_schedule(&bond_clear_work, K_NO_WAIT);

        /* We might reboot quickly, but try to ACK first */
        cmd_uart_send_line("OK");
        return;
    }

    if (!strcmp(line, "KB")) {
        if (!args) {
            cmd_uart_send_line("ERR -EINVAL");
            return;
        }
        uint8_t report8[8];
        if (!hex_to_bytes(args, report8, sizeof(report8))) {
            cmd_uart_send_line("ERR -EINVAL");
            return;
        }
        int rc = send_keyboard_report(report8);
        char b[32];
        snprintk(b, sizeof(b), (rc == 0) ? "OK" : "ERR %d", rc);
        cmd_uart_send_line(b);
        return;
    }

    if (!strcmp(line, "CC")) {
        if (!args) {
            cmd_uart_send_line("ERR -EINVAL");
            return;
        }
        uint8_t report2[2];
        if (!hex_to_bytes(args, report2, sizeof(report2))) {
            cmd_uart_send_line("ERR -EINVAL");
            return;
        }
        int rc = send_consumer_report(report2);
        char b[32];
        snprintk(b, sizeof(b), (rc == 0) ? "OK" : "ERR %d", rc);
        cmd_uart_send_line(b);
        return;
    }

    cmd_uart_send_line("ERR -EINVAL");
}


static void set_leds_adv(void)
{
    /* Advertising state: blue blink, all others off. */
    pihub_leds_all_off();
}

static void set_leds_conn(void)
{
    /* Connected: solid green. */
    pihub_led_conn_on();
}

static void set_leds_blink_adv(bool on)
{
    pihub_led_adv_set(on);
}


static void pihub_led_boot_sequence(void)
{
    for (int i = 0; i < 6; i++) {
        dk_set_led(PIHUB_LED_ERR,  i & 1);
        dk_set_led(PIHUB_LED_CONN, (i + 1) & 1);
        k_sleep(K_MSEC(120));
    }

    dk_set_led_off(PIHUB_LED_ERR);
    dk_set_led_off(PIHUB_LED_CONN);
}

static void blink_timer_handler(struct k_timer *timer)
{
    ARG_UNUSED(timer);
    blink_state = !blink_state;
    set_leds_blink_adv(blink_state);
}

static void start_advertising(void)
{
    /* Start (or restart) advertising via the work item so transient errors don’t leave us invisible. */
    k_work_schedule(&adv_restart_work, K_NO_WAIT);
}

static void log_conn_params_cached(const char *tag)
{
    if (!have_conn_params) {
        LOG_INF("%s: conn params not known yet", tag);
        return;
    }

    uint32_t interval_ms = ((uint32_t)last_interval * 125U) / 100U; /* 1.25 ms units -> ms */
    uint32_t timeout_ms  = (uint32_t)last_timeout * 10U;            /* 10 ms units -> ms */

    LOG_INF("%s: interval=%u ms latency=%u timeout=%u ms",
            tag,
            (unsigned)interval_ms,
            (unsigned)last_latency,
            (unsigned)timeout_ms);
}

static void connected(struct bt_conn *conn, uint8_t err)
{
    if (err) {
        LOG_ERR("Connect failed (err %u)", err);
        /* Keep advertising if connection setup failed. */
        start_advertising();
        return;
    }

    if (current_conn) {
        bt_conn_unref(current_conn);
    }
    current_conn = bt_conn_ref(conn);

    /* Reset per-connection state before recomputing readiness. */
    notify_kb_enabled = false;
    notify_cc_enabled = false;
    notify_boot_enabled = false;
    current_sec_level = BT_SECURITY_L1;
    hid_suspended = false;
    cmd_evt_err(false);
    update_link_ready("connected");

    k_timer_stop(&blink_timer);
    set_leds_conn();
    /* Stop advertising once connected (spec parity) */
    int adv_stop_err = bt_le_adv_stop();
    if ((adv_stop_err == 0) || (adv_stop_err == -EALREADY)) {
        cmd_evt_adv(false);
    }

    LOG_INF("Connected");
    update_link_ready("pm");
    log_conn_params_cached("Conn (initial)");


/* Let the central choose connection parameters. We only log what we get. */

    /* Ask for an encrypted link so iOS treats us like a real HID keyboard and allows bonding/UI. */
    int sec_err = bt_conn_set_security(conn, BT_SECURITY_L2);
    if (sec_err) {
        /* Not fatal; the peer may initiate pairing itself. */
        LOG_WRN("bt_conn_set_security failed: %d", sec_err);
    }
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
    ARG_UNUSED(conn);

    LOG_INF("Disconnected (reason %u)", reason);
    cmd_evt_adv(false);
    cmd_evt_disc(reason);
    cmd_evt_conn(false);

    if (current_conn) {
        bt_conn_unref(current_conn);
        current_conn = NULL;
    }

    notify_kb_enabled = false;
    notify_cc_enabled = false;
    notify_boot_enabled = false;
    current_sec_level = 0;
    hid_suspended = false;
    cmd_evt_err(false);
    update_link_ready("disconnected");

    set_leds_adv();
    k_timer_start(&blink_timer, K_NO_WAIT, K_MSEC(500));

    /* Re-advertise promptly so Apple devices can find us again after BT toggles. */
    start_advertising();
}

static void le_param_updated(struct bt_conn *conn, uint16_t interval,
                             uint16_t latency, uint16_t timeout)
{
    ARG_UNUSED(conn);

    /* Update CMD snapshot + emit EVT CONN_PARAMS with the new values */
    cmd_evt_conn_params(interval, latency, timeout);

    uint32_t interval_ms = ((uint32_t)interval * 125U) / 100U;
    uint32_t timeout_ms  = (uint32_t)timeout * 10U;

    LOG_INF("Conn params: interval=%u ms latency=%u timeout=%u ms",
            (unsigned)interval_ms,
            (unsigned)latency,
            (unsigned)timeout_ms);
}

static void security_changed(struct bt_conn *conn, bt_security_t level, enum bt_security_err err)
{
    if (err) {
        LOG_WRN("Security failed (level %u, err %d)", level, err);
        /* Don’t light RED for this; pairing can be retried from the phone. */
        return;
    }

    current_sec_level = level;
    cmd_evt_err(false);
    update_link_ready("security");
    LOG_INF("Security changed: level %u", level);

    if (level >= BT_SECURITY_L2) {
        int perr = bt_conn_le_param_update(conn, &low_latency_conn_params);
        if (perr && perr != -EALREADY) {
            LOG_WRN("Conn param update failed: %d", perr);
        } else {
            LOG_INF("Requested low-latency conn params");
        }
    }
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
    .connected = connected,
    .disconnected = disconnected,
    .security_changed = security_changed,
    .le_param_updated = le_param_updated,
};


static void auth_cancel(struct bt_conn *conn)
{
    ARG_UNUSED(conn);
    LOG_WRN("Pairing cancelled");
}

static void usb_console_init(void)
{
#if IS_ENABLED(CONFIG_USB_DEVICE_STACK) && !IS_ENABLED(CONFIG_USB_DEVICE_INITIALIZE_AT_BOOT)
    int usb_ret = usb_enable(NULL);

    if (usb_ret < 0 && usb_ret != -EALREADY) {
        LOG_WRN("usb_enable() failed: %d", usb_ret);
    } else {
        LOG_INF("USB enabled (%d)", usb_ret);
    }
#endif
}

/* Headless pairing model: no passkey/display/confirm callbacks => no input/no output. */
static struct bt_conn_auth_cb auth_cb = {
    .cancel = auth_cancel,
};

/* Startup order
 *
 * 1) visible hardware bring-up
 * 2) command/UART path
 * 3) button/work/timer init
 * 4) settings init
 * 5) Bluetooth enable + settings load
 * 6) enter advertising idle loop
 */
int main(void)
{
    int err;

    /* Short visible boot sequence so bring-up failures are obvious on bare hardware. */
    dk_leds_init();
    pihub_led_boot_sequence();
    usb_console_init();
    init_serial_num();

    /* Bring up CDC ACM command interface (PING/PONG + KB/CC). */
    cmd_uart_init();

    /* Set identity early (appearance is set via Kconfig: CONFIG_BT_DEVICE_APPEARANCE) */
    (void)bt_set_name(DEVICE_NAME);

    k_timer_init(&blink_timer, blink_timer_handler, NULL);

    k_work_init_delayable(&adv_restart_work, adv_restart_work_handler);
    k_work_init_delayable(&bond_clear_work, bond_clear_work_handler);
    k_work_init_delayable(&sw1_feedback_work, sw1_feedback_work_fn);

    /* Buttons: SW1 long-hold clears bonds */
    err = dk_buttons_init(button_changed);
    if (err) {
        LOG_WRN("dk_buttons_init failed (err %d) - will try direct GPIO sw0", err);
    }
#if DT_NODE_HAS_STATUS(SW1_NODE, okay)
    if (device_is_ready(sw1.port)) {
        int gerr = gpio_pin_configure_dt(&sw1, GPIO_INPUT | GPIO_PULL_UP);
        if (!gerr) {
            gerr = gpio_pin_interrupt_configure_dt(&sw1, GPIO_INT_EDGE_BOTH);
        }
        if (!gerr) {
            gpio_init_callback(&sw1_cb, sw1_gpio_isr, BIT(sw1.pin));
            gpio_add_callback(sw1.port, &sw1_cb);
            atomic_set(&sw1_pressed, 0);
            LOG_INF("SW1 GPIO handler armed (sw0)");
        } else {
            LOG_WRN("SW1 GPIO init failed (err %d)", gerr);
        }
    } else {
        LOG_WRN("SW1 GPIO device not ready");
    }
#else
    LOG_WRN("DT_ALIAS(sw0) not defined; SW1 long-press disabled");
#endif

    /*
     * IMPORTANT for persistence:
     * Init settings before bt_enable(). Otherwise bt_settings_init can fail
     * with -ENOENT if the backend/partition isn't available yet.
     */
#if IS_ENABLED(CONFIG_SETTINGS)
    err = pihub_settings_init_only();
    if (err) {
        LOG_ERR("settings_subsys_init failed (err %d)", err);
        pihub_leds_all_off();
        dk_set_led_on(PIHUB_LED_ERR);
        dk_set_led_on(PIHUB_LED_CONN);
        return 0;
    }
#endif

    err = bt_enable(NULL);
    if (err) {
        LOG_ERR("bt_enable failed (err %d)", err);
        printk("[PiHub] bt_enable failed (err %d)\n", err);
        /* Both LEDs ON solid means bt_enable failed */
        pihub_leds_all_off();
        dk_set_led_on(PIHUB_LED_ERR);
        dk_set_led_on(PIHUB_LED_CONN);
        return 0;
    }

    
    /* Load persisted settings (bonds/keys) if enabled */
#if IS_ENABLED(CONFIG_SETTINGS)
    err = pihub_settings_load_after_bt();
    if (err) {
        LOG_WRN("settings_load failed (err %d)", err);
    }
#endif

	bt_conn_auth_cb_register(&auth_cb);

    LOG_INF("Bluetooth ready");

    blink_state = false;
    set_leds_adv();
    k_timer_start(&blink_timer, K_NO_WAIT, K_MSEC(500));
    start_advertising();

    for (;;) {
        k_sleep(K_SECONDS(1));
    }
}
