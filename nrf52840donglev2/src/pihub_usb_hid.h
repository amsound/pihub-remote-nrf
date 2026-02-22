#pragma once
#include <stddef.h>
#include <stdint.h>

int pihub_usb_hid_init(void);
int pihub_usb_hid_write(const uint8_t *data, size_t len);
