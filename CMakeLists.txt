cmake_minimum_required(VERSION 3.22)
project(scrcpy_wifi_module LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Опції
option(ENABLE_VHCI "Enable real VHCI driver (requires usbip-win2)" OFF)
option(BUILD_TESTS "Build tests" ON)

# Попередження
if(MSVC)
    add_compile_options(/W4 /permissive- /EHsc)
    add_compile_definitions(_CRT_SECURE_NO_WARNINGS NOMINMAX WIN32_LEAN_AND_MEAN)
else()
    add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# Common — header-only + fec.cpp
add_library(common STATIC
    src/common/fec.cpp
)
target_include_directories(common PUBLIC src/common)
target_compile_features(common PUBLIC cxx_std_17)

# Agent-Receiver (Windows Host)
add_executable(agent_receiver
    src/agent_receiver/receiver_main.cpp
    src/agent_receiver/vhci_injector.cpp
    src/agent_receiver/adb_spoof.cpp
    src/agent_receiver/ghost_device.cpp
)
target_link_libraries(agent_receiver PRIVATE common)
target_include_directories(agent_receiver PRIVATE src/common)

if(WIN32)
    target_link_libraries(agent_receiver PRIVATE ws2_32 setupapi)
    # Для msquic (QUIC) — розкоментувати коли буде встановлено msquic
    # find_package(msquic CONFIG REQUIRED)
    # target_link_libraries(agent_receiver PRIVATE msquic)
    # target_compile_definitions(agent_receiver PRIVATE USE_QUIC)
endif()

# Agent-Sender (Android NDK / Windows Simulator)
add_executable(agent_sender
    src/agent_sender/sender_main.cpp
    src/agent_sender/adb_bridge.cpp
    src/agent_sender/gadget_stub.cpp
    # fec використовується для відео
)
target_link_libraries(agent_sender PRIVATE common)
target_include_directories(agent_sender PRIVATE src/common)

if(WIN32)
    target_link_libraries(agent_sender PRIVATE ws2_32)
endif()

# Android NDK — фейковий таргет для перевірки крос-компіляції
# Збірка для Android: cmake -B build-android -DCMAKE_TOOLCHAIN_FILE=$NDK/build/cmake/android.toolchain.cmake -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-26

# Тести
if(BUILD_TESTS)
    enable_testing()

    add_executable(test_fec tests/test_fec.cpp)
    target_link_libraries(test_fec PRIVATE common)

    add_executable(test_protocol tests/test_protocol.cpp)
    target_link_libraries(test_protocol PRIVATE common)

    add_executable(test_ringbuf tests/test_ringbuf.cpp)
    target_link_libraries(test_ringbuf PRIVATE common)
endif()

# Інфо
message(STATUS "=== scrcpy-wifi-module ===")
message(STATUS "System: ${CMAKE_SYSTEM_NAME}")
message(STATUS "Compiler: ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}")
message(STATUS "Build type: ${CMAKE_BUILD_TYPE}")
message(STATUS "VHCI: ${ENABLE_VHCI}")
message(STATUS "Tests: ${BUILD_TESTS}")