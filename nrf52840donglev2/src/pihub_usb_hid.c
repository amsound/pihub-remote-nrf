/*
 * PiHub USB HID link (legacy USB device stack).
 *
 * Vendor-defined HID interface used as a reliable transport between PiHub (host)
 * and the nRF52840 dongle (device).
 *
 * - OUT reports (host -> device) carry:
 *     - Binary hot path frames (0x01 + 8, 0x02 + 2)
 *     - ASCII commands (PING/STATUS/UNPAIR) newline-delimited
 * - IN reports (device -> host) carry ASCII lines (EVT/STATUS/OK/PONG/ERR...) newline-delimited
 *
 * This keeps the on-wire protocol identical to the earlier CDC ACM design, but
 * avoids tty/ACM re-enumeration issues.
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/usb/usb_device.h>
#include <zephyr/usb/class/usb_hid.h>
#include <zephyr/usb/class/hid.h>
#include <string.h>

extern void pihub_cmd_rx_bytes(const uint8_t *p, size_t n);

static const struct device *hdev;

/* Simple vendor-defined IN/OUT reports, 64 bytes each, no Report ID. */
static const uint8_t hid_report_desc[] = {
    HID_USAGE_PAGE(0xFF),                /* Vendor-defined */
    HID_USAGE(0x01),
    HID_COLLECTION(HID_COLLECTION_APPLICATION),

    /* OUT report */
    HID_LOGICAL_MIN8(0x00),
    HID_LOGICAL_MAX16(0xFF, 0x00),
    HID_REPORT_SIZE(8),
    HID_REPORT_COUNT(64),
    HID_USAGE(0x01),
    HID_OUTPUT(0x02),                    /* Data,Var,Abs */

    /* IN report */
    HID_REPORT_SIZE(8),
    HID_REPORT_COUNT(64),
    HID_USAGE(0x01),
    HID_INPUT(0x02),                     /* Data,Var,Abs */

    HID_END_COLLECTION,
};

static void status_cb(enum usb_dc_status_code status, const uint8_t *param)
{
    ARG_UNUSED(param);
    switch (status) {
    case USB_DC_CONFIGURED:
        /* Host configured us; nothing else required. */
        break;
    default:
        break;
    }
}

static void int_in_ready_cb(const struct device *dev)
{
    ARG_UNUSED(dev);
}

static void int_out_ready_cb(const struct device *dev)
{
    uint8_t buf[64];
    uint32_t n = 0;

    if (hid_int_ep_read(dev, buf, sizeof(buf), &n) == 0 && n > 0) {
        pihub_cmd_rx_bytes(buf, (size_t)n);
    }
}

static const struct hid_ops ops = {
    .int_in_ready = int_in_ready_cb,
    .int_out_ready = int_out_ready_cb,
};

int pihub_usb_hid_init(void)
{
    hdev = device_get_binding("HID_0");
    if (!hdev) {
        return -ENODEV;
    }

    usb_hid_register_device(hdev, hid_report_desc, sizeof(hid_report_desc), &ops);

    int ret = usb_hid_init(hdev);
    if (ret) {
        return ret;
    }

    return usb_enable(status_cb);
}

int pihub_usb_hid_write(const uint8_t *data, size_t len)
{
    if (!hdev || !data || len == 0) {
        return -EINVAL;
    }

    uint8_t pkt[64];
    memset(pkt, 0, sizeof(pkt));
    if (len > sizeof(pkt)) {
        len = sizeof(pkt);
    }
    memcpy(pkt, data, len);

    return hid_int_ep_write(hdev, pkt, sizeof(pkt), NULL);
}
