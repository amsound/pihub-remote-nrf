cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(pihub_minimal_adv_nrf52840dongle)

target_sources(app PRIVATE src/main.c)
