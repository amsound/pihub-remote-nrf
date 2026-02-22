cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(nrf52840donglev2)

target_sources(app PRIVATE src/main.c)

target_sources(app PRIVATE src/pihub_usb_hid.c)
