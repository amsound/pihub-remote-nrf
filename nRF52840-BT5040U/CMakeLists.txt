cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(nrf52840donglev2)

target_sources(app PRIVATE src/main.c)

# Build stamp reported by the INFO command:
#   <UTC build time>-<git commit>[-dirty][-<variant>]
# "dirty" means the firmware sources differ from that commit. It is worked out
# each time CMake configures; build.sh makes that happen on every build.
string(TIMESTAMP _stamp "%Y%m%d-%H%M" UTC)
execute_process(COMMAND git rev-parse --short HEAD
                WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
                OUTPUT_VARIABLE _commit OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
execute_process(COMMAND git status --porcelain -- src prj.conf CMakeLists.txt
                        nrf52840dongle.overlay pm_static.yml interval-11ms.conf
                WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
                OUTPUT_VARIABLE _changes OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
if(NOT _commit)
  set(_commit "nogit")
elseif(_changes)
  set(_commit "${_commit}-dirty")
endif()
set(PIHUB_FW_VERSION "${_stamp}-${_commit}")
if(EXTRA_CONF_FILE MATCHES "interval-11ms")
  set(PIHUB_FW_VERSION "${PIHUB_FW_VERSION}-11ms")
endif()
message(STATUS "PiHub firmware version: ${PIHUB_FW_VERSION}")
file(WRITE ${CMAKE_BINARY_DIR}/pihub_fw_version.txt "${PIHUB_FW_VERSION}")
target_compile_definitions(app PRIVATE FW_VERSION_STR="${PIHUB_FW_VERSION}")
