cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(nrf52840donglev2)

target_sources(app PRIVATE src/main.c)

# Build stamp reported by the INFO command: <UTC build time>-<git commit>[-dirty].
# build.sh passes PIHUB_FW_VERSION on every build; otherwise it is worked out here,
# when CMake configures (so a plain rebuild keeps the stamp of the last configure).
if(NOT PIHUB_FW_VERSION)
  string(TIMESTAMP _stamp "%Y%m%d-%H%M" UTC)
  execute_process(COMMAND git rev-parse --short HEAD
                  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
                  OUTPUT_VARIABLE _commit OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
  execute_process(COMMAND git diff --quiet HEAD -- .
                  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
                  RESULT_VARIABLE _dirty ERROR_QUIET)
  if(NOT _commit)
    set(_commit "nogit")
  elseif(_dirty)
    set(_commit "${_commit}-dirty")
  endif()
  set(PIHUB_FW_VERSION "${_stamp}-${_commit}")
endif()
message(STATUS "PiHub firmware version: ${PIHUB_FW_VERSION}")
target_compile_definitions(app PRIVATE FW_VERSION_STR="${PIHUB_FW_VERSION}")
