
/*
 * PiHub BLE HID Keyboard (minimal)
 * NCS v3.2.1 / Zephyr 4.2.x
 *
 * Diagnostics-first build:
 *  - Always blinks LEDs via dk_buttons_and_leds (works on nrf52840dongle)
 *  - BLE advertising is started after bt_enable()
 *  - Shows up as HID-over-GATT (HOGP) keyboard-class device (HID service 0x1812)
 */

#include <zephyr/kernel.h>
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
#define FW_VERSION_STR "0.1.0"
#endif
#endif

/* Forward declarations (avoid implicit int / non-static declarations under C99) */
static void start_advertising(void);
static const char *phy_to_str(uint8_t phy);


/* Erase the flash partition used by SETTINGS/NVS (label string: "storage").
 * This is used when the user long-presses SW1 to clear bonds + settings.
 */
static int pihub_erase_settings_storage(void)
{
    const struct flash_area *fa;
    int err = flash_area_open(FLASH_AREA_ID(storage), &fa);
    if (err) {
        LOG_ERR("flash_area_open(storage) failed: %d", err);
        return err;
    }

    err = flash_area_erase(fa, 0, fa->fa_size);
    flash_area_close(fa);

    if (err) {
        LOG_ERR("flash_area_erase(storage) failed: %d", err);
    } else {
        LOG_INF("Settings storage erased (%u bytes)", (unsigned int)fa->fa_size);
    }
    return err;
}


/* --- Simple HOGP: HID Service + a single input report characteristic --- */

/* HID Information (bcdHID=0x0111, country=0, flags=0x02 (normally remote wake)) */
/* HID Information (bcdHID=0x0111, country=0, flags=0x03 (remote wake + normally connectable)) */
static const uint8_t hid_info[] = { 0x11, 0x01, 0x00, 0x03 };

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

/* Forward decl: batt/CCC callbacks can fire before the helper is defined. */
static void update_link_ready(const char *why);

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




static uint8_t protocol_mode = 1; /* 0=Boot, 1=Report */
/* Cached link status for STATUS snapshots (so host can resync after restart) */
static bool have_conn_params;
static uint16_t last_interval;
static uint16_t last_latency;
static uint16_t last_timeout;

static bool have_phy;
static uint8_t last_tx_phy;
static uint8_t last_rx_phy;
/* Reports (Report IDs are indicated via Report Reference descriptors; payloads here do NOT include the ID byte) */
static uint8_t kb_report[8] = { 0 };      /* modifiers, reserved, 6 keys */
static uint8_t cc_report[2] = { 0 };      /* 16-bit Consumer usage */
static uint8_t boot_kb_report[8] = { 0 };


/* Report Reference descriptor payload: [Report ID, Report Type (Input=1)] */

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

/* --------------------------------------------------------------------------
 * USB CDC ACM command channel (PiHub <-> dongle)
 *
 * We expose a simple newline-delimited ASCII protocol on CDC ACM 0:
 *   PING            -> PONG
 *   STATUS          -> STATUS adv=<0/1> conn=<0/1> proto=<0/1> err=<0/1>
 *   UNPAIR          -> OK (clears bonds + restarts advertising)
 *   KB <16hex>      -> OK|BUSY|ERR <rc>
 *   CC <4hex>       -> OK|BUSY|ERR <rc>
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

static void cmd_uart_send_str(const char *s)
{
    if (!atomic_get(&cmd_uart_ready) || (cmd_uart == NULL)) {
        return;
    }
    for (const char *p = s; *p; p++) {
        uart_poll_out(cmd_uart, (unsigned char)*p);
    }
}

static void cmd_uart_send_line(const char *s)
{
    cmd_uart_send_str(s);
    cmd_uart_send_str("\n");
}

static void fmt_interval_ms(char *dst, size_t dst_len, uint16_t interval_units)
{
    /* interval_units is 1.25 ms units (per BT spec). We format as xx.yy (2dp) without floats. */
    uint32_t ms_x100 = (uint32_t)interval_units * 125U; /* 1.25ms -> 125/100 */
    snprintk(dst, dst_len, "%u.%02u", (unsigned)(ms_x100 / 100U), (unsigned)(ms_x100 % 100U));
}



static void cmd_evt_adv(bool on)      { adv_is_on = on; char b[24]; snprintk(b, sizeof(b), "EVT ADV %d", on ? 1 : 0); cmd_uart_send_line(b); }
static void cmd_evt_conn(bool on)     { char b[24]; snprintk(b, sizeof(b), "EVT CONN %d", on ? 1 : 0); cmd_uart_send_line(b); }
static void cmd_evt_ready(bool on)    { char b[32]; snprintk(b, sizeof(b), "EVT READY %d", on ? 1 : 0); cmd_uart_send_line(b); }
static void cmd_evt_proto(uint8_t pm) { char b[24]; snprintk(b, sizeof(b), "EVT PROTO %u", (unsigned int)(pm ? 1 : 0)); cmd_uart_send_line(b); }
static void cmd_evt_err(bool on)      { error_state = on; char b[24]; snprintk(b, sizeof(b), "EVT ERR %d", on ? 1 : 0); cmd_uart_send_line(b); }

/* --- Forward decls --- */
static void update_link_ready(const char *why);
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
    char interval_s[16];
    fmt_interval_ms(interval_s, sizeof(interval_s), interval);

    uint32_t timeout_ms = (uint32_t)timeout * 10U; /* 10ms units -> ms */
    snprintk(b, sizeof(b), "EVT CONN_PARAMS interval_ms=%s latency=%u timeout_ms=%u",
             interval_s, (unsigned)latency, (unsigned)timeout_ms);
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

static void cmd_emit_status_snapshot(void)
{
    /* Recompute readiness on demand so a PiHub restart can resync state even
     * if READY transitioned earlier (or if notifications were enabled after
     * connect/security).
     */
    bool now_ready = (current_conn != NULL) &&
                     (current_sec_level >= BT_SECURITY_L2) &&
                     !hid_suspended &&
                     (notify_kb_enabled || notify_boot_enabled);

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

    /* Human-readable conn params (no floats). */
    char interval_s[16] = "0.00";   /* was "?" */
    uint32_t timeout_ms = 0;

    if (have_conn_params) {
        fmt_interval_ms(interval_s, sizeof(interval_s), last_interval);
        timeout_ms = (uint32_t)last_timeout * 10U; /* 10ms units -> ms */
    }

    snprintk(b, sizeof(b),
             "STATUS adv=%d conn=%d sec=%u ready=%d proto=%u err=%d kb_notify=%d boot_notify=%d cc_notify=%d batt_notify=%d "
             "suspend=%d interval_ms=%s latency=%u timeout_ms=%u phy_tx=%u phy_rx=%u",
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
             interval_s,
             (unsigned)last_latency,
             (unsigned)timeout_ms,
             (unsigned)last_tx_phy,
             (unsigned)last_rx_phy);

    cmd_uart_send_line(b);
}


static void update_link_ready(const char *why)
{
    bool now = (current_conn != NULL) &&
               (current_sec_level >= BT_SECURITY_L2) &&
               (!hid_suspended) &&
               (notify_kb_enabled || notify_boot_enabled);

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

/* Command RX thread */
#define CMD_RX_STACK 1536
#define CMD_RX_PRIO  5
K_THREAD_STACK_DEFINE(cmd_rx_stack, CMD_RX_STACK);
static struct k_thread cmd_rx_thread;

static void cmd_rx_thread_fn(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);

    if (cmd_uart == NULL) {
        return;
    }

    /* Wait for host to open the port (DTR). */
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
    cmd_uart_send_line("EVT BOOT pihub-hids");

    char line[96];
    size_t n = 0;

    while (1) {
        uint8_t ch;
        int rc = uart_poll_in(cmd_uart, &ch);
        if (rc == 0) {
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
                /* Overflow: drop line. */
                n = 0;
            }
        } else {
            k_msleep(5);
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


/* --- SW1 long-press bond clear (5 seconds) --- */
/* Prefer direct GPIO for SW1: more reliable than dk_buttons on nrf52840dongle. */
#define SW1_NODE DT_ALIAS(sw0)
#if DT_NODE_HAS_STATUS(SW1_NODE, okay)
static const struct gpio_dt_spec sw1 = GPIO_DT_SPEC_GET(SW1_NODE, gpios);
static struct gpio_callback sw1_cb;
static atomic_t sw1_pressed;

/* --------------------------------------------------------------------------
 * SETTINGS/NVS storage helpers
 *
 * Why this exists:
 * - With CONFIG_BT_SETTINGS=y, bt_enable() will try to init the settings
 *   subsystem. If the settings backend can't open the flash area, bt_enable()
 *   fails and the app never advertises (you've been seeing solid green+red).
 * - On the dongle you only have USB DFU, so "west flash --erase" (debug-probe
 *   mass erase) isn't available. We therefore provide an *in-firmware* erase of
 *   the settings partition.
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
    if (fa) {
        flash_area_close(fa);
    }
}

static int pihub_erase_storage(void)
{
    const struct flash_area *fa = NULL;
    int err = pihub_open_storage(&fa);
    if (err) {
        return err;
    }
    err = flash_area_erase(fa, 0, fa->fa_size);
    pihub_close_storage(fa);
    return err;
}

static void pihub_storage_banner(void)
{
    const struct flash_area *fa = NULL;
    int err = pihub_open_storage(&fa);
    if (err) {
        printk("[PiHub] storage area open failed (err %d)\n", err);
        return;
    }
    printk("[PiHub] storage area: off=0x%lx size=0x%lx\n",
           (unsigned long)fa->fa_off,
           (unsigned long)fa->fa_size);
    pihub_close_storage(fa);
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


static struct k_work_delayable bond_clear_work;
static void bond_clear_work_handler(struct k_work *work);
static void button_changed(uint32_t button_state, uint32_t has_changed);
static void set_leds_adv(void);
static struct k_timer blink_timer;


static struct k_work_delayable adv_restart_work;

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

    protocol_mode = ((const uint8_t *)buf)[0] ? 1 : 0;
    LOG_INF("Protocol Mode set: 0x%02x (%s)", protocol_mode,
            protocol_mode ? "Report" : "Boot");
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
 * Hosts use this for things like "Suspend"/"Exit Suspend" in some HID profiles.
 * We don't need special handling; accept the write to keep hosts happy.
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
    LOG_INF("HID Control Point write: 0x%02x", cp[0]);
    return len;
}

/* HID Service (0x1812) */
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


/* Device Information Service (helps some UIs show “keyboard” properly) */
static const char mfg_name[] = "PiHub";
static const char model_num[] = "PiHub Keyboard";

static ssize_t read_str(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                        void *buf, uint16_t len, uint16_t offset)
{
    const char *s = attr->user_data;
    return bt_gatt_attr_read(conn, attr, buf, len, offset, s, (uint16_t)strlen(s));
}

/* --- Device Information Service (0x180A) --- */
static const char dis_manufacturer[] = "PiHub";
static const char dis_model[]        = "nRF52840 Dongle";
static const char dis_serial[]       = "PIHUB-0001";
static const char dis_fw_rev[]       = "0.1.0";

BT_GATT_SERVICE_DEFINE(dis_svc,
    BT_GATT_PRIMARY_SERVICE(BT_UUID_DIS),
    BT_GATT_CHARACTERISTIC(BT_UUID_DIS_MANUFACTURER_NAME,
                           BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ,
                           read_str, NULL, (void *)mfg_name),
    BT_GATT_CHARACTERISTIC(BT_UUID_DIS_MODEL_NUMBER,
                           BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ,
                           read_str, NULL, (void *)model_num)
);

/* Advertising data: Flags + HID service UUID (16-bit) */
static const struct bt_data ad[] = {
    BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
    /* Advertise as HID (Keyboard) and Battery in the 16-bit UUID list */
    BT_DATA_BYTES(BT_DATA_UUID16_ALL,
                  0x12, 0x18, /* 0x1812 HID */
                  0x0F, 0x18  /* 0x180F Battery */
                  ),
    /* GAP Appearance AD type (0x19): 0x03C1 = Keyboard */
    BT_DATA_BYTES(BT_DATA_GAP_APPEARANCE, 0xC1, 0x03),
};

/* Put the device name in scan response so platforms reliably show “PiHub”. */
static const struct bt_data sd[] = {
    BT_DATA(BT_DATA_NAME_COMPLETE, "PiHub", 5),
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
        /* tiny feedback on press */
        dk_set_led_on(DK_LED2);
        k_sleep(K_MSEC(40));
        dk_set_led_off(DK_LED2);

        k_work_schedule(&bond_clear_work, K_SECONDS(5));
    } else {
        (void)k_work_cancel_delayable(&bond_clear_work);
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
	if (!current_conn || !notify_cc_enabled) {
		return -ENOTCONN;
	}
	if (current_sec_level < BT_SECURITY_L2) {
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

    /* Best-effort “all keys up” on both Keyboard + Consumer.
     * Ignore errors: link may be busy, not ready, or notifications disabled.
     */
    uint8_t zeros8[8] = { 0 };
    uint8_t zeros2[2] = { 0 };

    (void)send_keyboard_report(zeros8);
    (void)send_consumer_report(zeros2);

    /* Optional tiny gap to let the notify path drain */
    k_msleep(10);

    (void)send_keyboard_report(zeros8);
    (void)send_consumer_report(zeros2);
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

        /* Remove all bonds/keys from BT layer (then we also erase the backing store). */
        (void)bt_unpair(BT_ID_DEFAULT, BT_ADDR_LE_ANY);

        int rc = pihub_erase_settings_storage();
        if (rc) {
            char b[32];
            snprintk(b, sizeof(b), "ERR %d", rc);
            cmd_uart_send_line(b);
        } else {
            cmd_uart_send_line("OK");
            /* Slightly longer delay than before helps hosts process disconnect/unpair cleanly. */
            k_msleep(300);
            sys_reboot(SYS_REBOOT_COLD);
        }
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
    /* Advertising: BLUE (LED1) will blink via timer, RED off */
    dk_set_led_off(DK_LED4);
    dk_set_led_off(DK_LED3);
    dk_set_led_off(DK_LED2);
    dk_set_led_off(DK_LED1);

    /*
     * USB CDC ACM console:
     * If CONFIG_USB_DEVICE_INITIALIZE_AT_BOOT=y, Zephyr already enables USB
     * and calling usb_enable() again will return -EALREADY.
     */
#if IS_ENABLED(CONFIG_USB_DEVICE_STACK) && !IS_ENABLED(CONFIG_USB_DEVICE_INITIALIZE_AT_BOOT)
    int usb_ret = usb_enable(NULL);
    if (usb_ret < 0 && usb_ret != -EALREADY) {
        LOG_WRN("usb_enable() failed: %d", usb_ret);
    }
#endif
    dk_set_led(DK_LED1, 0);
}

static void set_leds_conn(void)
{
    /* Connected: solid GREEN (LED1), RED off */
    dk_set_led_off(DK_LED2);
    dk_set_led(DK_LED1, 1);
}

static void set_leds_blink_adv(bool on)
{
    dk_set_led(DK_LED4, on ? 1 : 0);
}

static bool blink_state;

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

static void log_conn_params(struct bt_conn *conn, const char *tag)
{
    struct bt_conn_info info;
    int err = bt_conn_get_info(conn, &info);
    if (err) {
        LOG_WRN("%s: bt_conn_get_info failed: %d", tag, err);
        return;
    }
    if (info.type != BT_CONN_TYPE_LE) {
        LOG_INF("%s: non-LE connection", tag);
        return;
    }

    /* interval units: 1.25 ms, timeout units: 10 ms */
    uint32_t interval_ms_x100 = (uint32_t)info.le.interval * 125U; /* 1.25ms -> 125/100 */
    uint16_t latency = info.le.latency;
    uint32_t timeout_ms = (uint32_t)info.le.timeout * 10U;

    cmd_evt_conn_params(info.le.interval, info.le.latency, info.le.timeout);

    LOG_INF("%s: interval=%u.%02u ms latency=%u timeout=%u ms",
            tag,
            (unsigned)(interval_ms_x100 / 100U),
            (unsigned)(interval_ms_x100 % 100U),
            latency,
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

    update_link_ready("connected");
    /* Reset per-connection state */
    notify_kb_enabled = false; notify_cc_enabled = false; notify_boot_enabled = false;
    current_sec_level = BT_SECURITY_L1;

    k_timer_stop(&blink_timer);
    set_leds_conn();
    /* Stop advertising once connected (spec parity) */
    int adv_stop_err = bt_le_adv_stop();
    if ((adv_stop_err == 0) || (adv_stop_err == -EALREADY)) {
        cmd_evt_adv(false);
    }

    LOG_INF("Connected");
    hid_suspended = false;
    update_link_ready("pm");
    log_conn_params(conn, "Conn (initial)");


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
    update_link_ready("disconnected");
    }

    notify_kb_enabled = false; notify_cc_enabled = false; notify_boot_enabled = false;
    current_sec_level = 0;

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

    uint32_t interval_ms_x100 = (uint32_t)interval * 125U;
    uint32_t timeout_ms = (uint32_t)timeout * 10U;

    LOG_INF("Conn (updated): interval=%u.%02u ms latency=%u timeout=%u ms",
            (unsigned)(interval_ms_x100 / 100U),
            (unsigned)(interval_ms_x100 % 100U),
            latency,
            (unsigned)timeout_ms);
}




static void security_changed(struct bt_conn *conn, bt_security_t level, enum bt_security_err err)
{
    ARG_UNUSED(conn);

    if (err) {
        LOG_WRN("Security failed (level %u, err %d)", level, err);
        /* Don’t light RED for this; pairing can be retried from the phone. */
        return;
    }

    current_sec_level = level;
    update_link_ready("security");
    LOG_INF("Security changed: level %u", level);

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

static struct bt_conn_auth_cb auth_cb = {
    .cancel = auth_cancel,
};

int main(void)
{
    int err;

    /* Hard “I am alive” sequence on boot (always visible) */
    dk_leds_init();
    for (int i = 0; i < 6; i++) {
        dk_set_led(DK_LED1, i & 1);
        dk_set_led(DK_LED2, (i + 1) & 1);
        k_sleep(K_MSEC(120));
    }
    dk_set_led_off(DK_LED2);

	    /*
	     * USB CDC ACM console:
	     * If CONFIG_USB_DEVICE_INITIALIZE_AT_BOOT=y, Zephyr already enables USB
	     * and calling usb_enable() again will return -EALREADY.
	     */
	#if IS_ENABLED(CONFIG_USB_DEVICE_STACK) && !IS_ENABLED(CONFIG_USB_DEVICE_INITIALIZE_AT_BOOT)
	    {
	        int usb_ret = usb_enable(NULL);
	        if (usb_ret < 0 && usb_ret != -EALREADY) {
	            LOG_WRN("usb_enable() failed: %d", usb_ret);
	        } else {
	            LOG_INF("USB enabled (%d)", usb_ret);
	        }
	    }
	#endif

    /* Bring up CDC ACM command interface (PING/PONG + KB/CC). */
    cmd_uart_init();

    /* Set identity early (appearance is set via Kconfig: CONFIG_BT_DEVICE_APPEARANCE) */
    (void)bt_set_name("PiHub");

    k_timer_init(&blink_timer, blink_timer_handler, NULL);

    k_work_init_delayable(&adv_restart_work, adv_restart_work_handler);
    k_work_init_delayable(&bond_clear_work, bond_clear_work_handler);

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
        dk_set_led_on(DK_LED1);
        dk_set_led_on(DK_LED2);
        return 0;
    }
#endif

    err = bt_enable(NULL);
    if (err) {
        LOG_ERR("bt_enable failed (err %d)", err);
        printk("[PiHub] bt_enable failed (err %d)\n", err);
        /* Both LEDs ON solid means bt_enable failed */
        dk_set_led_on(DK_LED1);
        dk_set_led_on(DK_LED2);
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
