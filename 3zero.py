#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Filename: 3zero.py
Description:
A camera app for the Raspberry Pi Zero 2 W (should also run on a 4 or 5) with a Raspberry Camer HQ and a 4" Waveshare touch screen
Runs a live preview with button bars for exposure control, shutter release, self timer, WiFi On/Off, AP Mode, Shutdown
Images saved to /srv/DCIM (for sharing with samba)

Author: Oliver Scheifinger, @nologo_earth
License: GPL3
Created: 2025-03-30
Last Modified: 2025-05-11
Python Version: 3

Usage:
    python3 3zero.py

Notes:
    - Needs a graphical desktop environment (e.g. Wayland) to run. Terminal to run must be open within desktop environment. Starting from command line does not work.
"""

import sys

# Ensure necessary modules and classes are imported
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QMessageBox
)
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen
from picamera2 import Picamera2
from libcamera import controls
import numpy as np
import os
import time
from gpiozero import Button
import subprocess
import traceback
import atexit

# Camera Constants and Mappings
exposure_times = {
    '1': 1000000, '1/2': 500000, '1/4': 250000, '1/15': 66667,
    '1/30': 33333, '1/60': 16667, '1/125': 8000, '1/250': 4000,
    '1/500': 2000, '1/1000': 1000,
}
button_pin = 26 # GPIO pin for the external, physical shutter release button
TIMER_DELAY_MS = 10000 # 10 seconds for self-timer
BUTTON_BAR_HEIGHT = 36 # Height of the button bars in pixels - Change to scale for different screens / resolutions

# Network Configuration Constants
WIFI_CLIENT_CONNECTION_NAME = "preconfigured" # Name of the saved WiFi client connection in NetworkManager when set with Raspberry Imager, change if you use a different one
AP_SSID = "3zero" # SSID for the Access Point
AP_PASSWORD = "3zerocamera" # Password for the Access Point
AP_CONNECTION_NAME = "CameraHotspot" # Internal name for the temporary AP connection profile in NetworkManager
AP_STABILIZE_DELAY_S = 15 # Increased delay after starting AP for clean samba restart

# --- Setup Picamera2 ---
picam2 = Picamera2()
general_settings = { # Default settings (Auto Exposure)
    "AeEnable": True,
    "AeMeteringMode": controls.AeMeteringModeEnum.Matrix,
    "AwbEnable": True,
    "AwbMode": controls.AwbModeEnum.Auto,
    "AeConstraintMode": controls.AeConstraintModeEnum.Normal,
    "AeExposureMode": controls.AeExposureModeEnum.Normal,
}
preview_config = picam2.create_preview_configuration(
    main={"size": (960, 720)}, controls=general_settings
)
picam2.configure(preview_config)
print("Starting Picamera2...")
picam2.start()
print("Picamera2 started.")

# Helper function to run system commands
def run_system_command(command_list, ignore_fail=False):
    """Runs a system command using subprocess and returns True on success."""
    try:
        print(f"Executing: {' '.join(command_list)}")
        # Using check=False and evaluating returncode manually
        result = subprocess.run(command_list, check=False, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"Command successful.")
            return True
        else:
            # Log error even if ignoring failure for subsequent steps
            print(f"Error executing command (Code: {result.returncode}):")
            if result.stderr: print(f"Stderr:\n{result.stderr.strip()}")
            if result.stdout: print(f"Stdout:\n{result.stdout.strip()}")
            # Return True if failure is explicitly ignored, otherwise False
            return True if ignore_fail else False
    except FileNotFoundError:
         print(f"Error: Command '{command_list[0]}' not found.")
         return False
    except subprocess.TimeoutExpired:
         print(f"Error: Command {' '.join(command_list)} timed out.")
         return False
    except Exception as e:
         print(f"An unexpected error occurred with {' '.join(command_list)}: {e}")
         traceback.print_exc()
         return False

# Ensure WiFi Client is UP and Samba ON at Script Startup
print("Ensuring WiFi radio and Client connection are active at startup...")
initial_services_ok = True
startup_commands = [
    ['sudo', '/usr/sbin/rfkill', 'unblock', 'wifi'],
    ['sudo', '/usr/bin/nmcli', 'connection', 'up', WIFI_CLIENT_CONNECTION_NAME],
    ['sudo', '/bin/systemctl', 'start', 'nmbd'],
    ['sudo', '/bin/systemctl', 'start', 'smbd']
]
for i, cmd in enumerate(startup_commands):
    # Ignore failure only for 'nmcli connection up' (index 1)
    allow_fail = (i == 1)
    if not run_system_command(cmd, ignore_fail=allow_fail):
        # If any command other than 'nmcli up' fails, mark startup as potentially problematic
        if not allow_fail:
            initial_services_ok = False
if initial_services_ok:
    print("WiFi radio unblocked, Client connection up (or already up), Samba started.")
else:
    print("Warning: One or more critical startup commands failed.")

# Global State Variables
active_exposure_button = None # Tracks the currently active exposure button widget
is_timer_countdown_active = False # Tracks if the self-timer is running
is_wifi_on = initial_services_ok # Master WiFi state (radio on/off)
is_ap_mode_active = False # Tracks if AP mode is intended to be active
current_manual_settings = None # Stores the dict of manual settings if active, else None

# Common Button Style Sheet
button_style_sheet = """
    QPushButton {
        background-color: black; color: white; font-size: 14px;
        font-weight: bold; border: none; padding: 2px;
    }
    QPushButton:hover { background-color: #555; }
    QPushButton:disabled { background-color: #222; color: #777; }
    """

# Style sheet for active buttons (red text)
active_style_sheet_red = button_style_sheet.replace("color: white;", "color: red;")

# Network Mode Control Functions (implemented with nmcli)
def start_client_mode():
    #Sequence to specifically activate WiFi client mode using nmcli.
    print("FUNC: Attempting to start Client Mode...")
    success = True
    # Ensure WiFi radio is unblocked
    if not run_system_command(['sudo', '/usr/sbin/rfkill', 'unblock', 'wifi']): success = False

    # Re-enable and start dnsmasq if needed when going to Client Mode
    # These might fail if dnsmasq isn't installed or managed by systemd, ignore failure.
    print("--> Ensuring dnsmasq is enabled/started (if applicable for client mode)...")
    run_system_command(['sudo', '/bin/systemctl', 'enable', 'dnsmasq'], ignore_fail=True)
    run_system_command(['sudo', '/bin/systemctl', 'start', 'dnsmasq'], ignore_fail=True)

    # Attempt to bring up the preconfigured client connection
    # Allow this to fail gracefully if already up or configuration is missing
    if success and not run_system_command(['sudo', '/usr/bin/nmcli', 'connection', 'up', WIFI_CLIENT_CONNECTION_NAME], ignore_fail=True):
        print(f"INFO: nmcli connection up {WIFI_CLIENT_CONNECTION_NAME} finished (may have failed if already up or not configured).")

    # Start Samba services for client mode file sharing
    if success and not run_system_command(['sudo', '/bin/systemctl', 'start', 'nmbd']): success = False
    if success and not run_system_command(['sudo', '/bin/systemctl', 'start', 'smbd']): success = False

    return success

def stop_client_mode():
    #Sequence to specifically deactivate WiFi client mode using nmcli.
    print("FUNC: Attempting to stop Client Mode...")
    # Attempt to bring down the client connection, ignore failure if not up
    run_system_command(['sudo', '/usr/bin/nmcli', 'connection', 'down', WIFI_CLIENT_CONNECTION_NAME], ignore_fail=True)
    # Stop Samba services
    run_system_command(['sudo', '/bin/systemctl', 'stop', 'smbd'], ignore_fail=True)
    run_system_command(['sudo', '/bin/systemctl', 'stop', 'nmbd'], ignore_fail=True)
    # Note: We don't stop/disable dnsmasq here, only when starting AP mode.
    return True # Assume success for stopping services

# Start_ap_mode: Added dnsmasq stop/disable and re-enabled Samba start
def start_ap_mode():
    #Sequence to enable WiFi Access Point mode using nmcli hotspot.
    print("FUNC: Attempting to start AP Mode...")
    success = True
    hotspot_started = False

    # Ensure WiFi radio is unblocked
    if not run_system_command(['sudo', '/usr/sbin/rfkill', 'unblock', 'wifi']):
        print("ERROR: Failed to unblock wifi radio in start_ap_mode.")
        success = False

    # Stop and disable dnsmasq before starting nmcli hotspot
    if success:
        print("--> Stopping dnsmasq (if running)...")
        # Ignore failure if dnsmasq is not running or not installed
        run_system_command(['sudo', '/bin/systemctl', 'stop', 'dnsmasq'], ignore_fail=True)
        print("--> Disabling dnsmasq (to prevent conflicts)...")
        # Ignore failure if dnsmasq cannot be disabled
        run_system_command(['sudo', '/bin/systemctl', 'disable', 'dnsmasq'], ignore_fail=True)

    # Attempt to start the nmcli hotspot
    if success:
        if run_system_command([
            'sudo', '/usr/bin/nmcli', 'device', 'wifi', 'hotspot',
            'ifname', 'wlan0', 'con-name', AP_CONNECTION_NAME,
            'ssid', AP_SSID, 'password', AP_PASSWORD
        ]):
            hotspot_started = True
        else:
            print("ERROR: Failed to start nmcli hotspot.")
            success = False

    # If hotspot started, WAIT before starting Samba
    if success and hotspot_started:
        print(f"Waiting {AP_STABILIZE_DELAY_S} seconds for AP network to stabilize...")
        time.sleep(AP_STABILIZE_DELAY_S) # Use constant for delay

        # Re-enabled Samba Start for AP Mode
        print("--> Starting Samba services (nmbd, smbd) for AP mode...")
        if not run_system_command(['sudo', '/bin/systemctl', 'start', 'nmbd']):
            print("ERROR: Failed to start nmbd in AP mode.")
            success = False # Mark as failure if Samba doesn't start
        if not run_system_command(['sudo', '/bin/systemctl', 'start', 'smbd']):
            print("ERROR: Failed to start smbd in AP mode.")
            success = False # Mark as failure if Samba doesn't start
        # End Re-enabled Section

    # If any critical step failed, return False
    return success

def stop_ap_mode():
    #Sequence to disable WiFi Access Point mode created by nmcli.
    print("FUNC: Attempting to stop AP Mode...")
    # Bring down and delete the temporary AP connection
    run_system_command(['sudo', '/usr/bin/nmcli', 'connection', 'down', AP_CONNECTION_NAME], ignore_fail=True)
    run_system_command(['sudo', '/usr/bin/nmcli', 'connection', 'delete', AP_CONNECTION_NAME], ignore_fail=True)
    # Stop Samba services if they were running in AP mode
    run_system_command(['sudo', '/bin/systemctl', 'stop', 'smbd'], ignore_fail=True)
    run_system_command(['sudo', '/bin/systemctl', 'stop', 'nmbd'], ignore_fail=True)
    # Note: We don't explicitly re-enable/start dnsmasq here.
    # It will be handled by start_client_mode if switching back.
    return True # Assume success for stopping services
# End Network Mode Control Functions

# Handler Functions
def reapply_manual_exposure_if_needed():
    """Checks if manual exposure was active and re-applies it."""
    global current_manual_settings, picam2
    if current_manual_settings:
        try:
            print("Re-applying manual exposure settings after capture...")
            picam2.set_controls(current_manual_settings)
            print("Manual exposure re-applied.")
        except Exception as e:
            print(f"Error re-applying manual exposure settings: {e}")

def on_save_button_clicked():
    # Handles clicks on the Save ('O') button.
    global is_timer_countdown_active
    if is_timer_countdown_active:
        print("Save button ignored, timer is active.")
        return # Do nothing if timer is running
    else:
        print("GUI Save ('O') button pressed, saving image immediately.")
        save_image()
        # Re-apply manual exposure if it was set
        reapply_manual_exposure_if_needed()

def on_timer_button_clicked():
    #Handles clicks on the Timer ('10s') button.
    global is_timer_countdown_active, btn_timer
    if is_timer_countdown_active:
        # Cancel the timer
        is_timer_countdown_active = False
        # No need to explicitly stop QTimer.singleShot, just prevent action in callback
        btn_timer.setStyleSheet(button_style_sheet) # Reset style using original base style
        print("Self-timer cancelled.")
    else:
        # Start the timer
        is_timer_countdown_active = True
        btn_timer.setStyleSheet(active_style_sheet_red) # Active style (red text)
        print("Self-timer started (10s)...")
        QTimer.singleShot(TIMER_DELAY_MS, delayed_capture_and_reset)

def on_wifi_button_clicked():
    #Handles clicks on the 'WiFi' button as master ON/OFF switch.
    global is_wifi_on, is_ap_mode_active, btn_wifi, btn_ap
    target_state_on = not is_wifi_on # Determine desired state

    if target_state_on:
        print("WiFi button: Turning ON (Activating Client mode)...")
        # Ensure AP mode state variable is false and button looks inactive
        is_ap_mode_active = False
        if btn_ap: btn_ap.setStyleSheet(button_style_sheet) # Use original base style

        if start_client_mode():
            print("WiFi ON (Client Mode).")
            is_wifi_on = True
            btn_wifi.setStyleSheet(active_style_sheet_red) # Active style
            if btn_ap: btn_ap.setEnabled(True) # Enable AP button
        else:
            print("ERROR: Failed to start Client mode services. Turning radio off.")
            run_system_command(['sudo', '/usr/sbin/rfkill', 'block', 'wifi']) # Block radio on failure
            is_wifi_on = False
            btn_wifi.setStyleSheet(button_style_sheet) # Inactive style
            if btn_ap: btn_ap.setEnabled(False) # Disable AP button
    else:
        # Turning WiFi OFF
        print("WiFi button: Turning OFF...")
        stop_success = False
        if is_ap_mode_active:
            print("Stopping AP mode services...")
            stop_success = stop_ap_mode() # Calls function with real commands
        else:
            # If not in AP mode, must be in Client mode (or trying to be)
            print("Stopping Client mode services...")
            stop_success = stop_client_mode() # Calls function with real commands

        print("Blocking WiFi radio...")
        rfkill_success = run_system_command(['sudo', '/usr/sbin/rfkill', 'block', 'wifi'])

        if stop_success and rfkill_success:
            print("WiFi OFF.")
            is_wifi_on = False
            is_ap_mode_active = False # Ensure AP state is also off
            btn_wifi.setStyleSheet(button_style_sheet) # Inactive style for WiFi button
            if btn_ap:
                btn_ap.setStyleSheet(button_style_sheet) # Inactive style for AP button
                btn_ap.setEnabled(False) # Disable AP button
        else:
            print("ERROR: Failed to properly turn off WiFi/Services. State may be inconsistent.")
            # Attempt to revert visual state if turn-off failed
            is_wifi_on = True # Assume it's still effectively on
            btn_wifi.setStyleSheet(active_style_sheet_red) # Active style
            if btn_ap: btn_ap.setEnabled(True) # Keep AP button enabled

def on_ap_button_clicked():
    #Handles clicks on the 'AP' button to switch network mode.
    global is_ap_mode_active, is_wifi_on, btn_ap
    if not is_wifi_on:
        print("AP button clicked, but WiFi is OFF. Ignoring.")
        return # Do nothing if WiFi master switch is off

    target_ap_on = not is_ap_mode_active # Determine desired AP state

    success = False
    if target_ap_on:
        # Switching Client -> AP
        print("AP button: Attempting switch Client -> AP mode...")
        if stop_client_mode():
            # Now calls start_ap_mode which includes starting Samba
            if start_ap_mode():
                success = True
            else:
                print("ERROR: Failed to start AP mode services after stopping client. Attempting to revert to Client mode...")
                start_client_mode() # Try to go back to client mode
        else:
            print("ERROR: Failed to stop client mode services before starting AP.")

        if success:
            print("Successfully switched to AP mode.")
            is_ap_mode_active = True
            btn_ap.setStyleSheet(active_style_sheet_red) # Active style
        else:
            print("Failed to switch to AP mode. Reverting selection visuals.")
            is_ap_mode_active = False # Stay in client mode logically
            btn_ap.setStyleSheet(button_style_sheet) # Inactive style
    else:
        # Switching AP -> Client
        print("AP button: Attempting switch AP -> Client mode...")
        if stop_ap_mode():
            if start_client_mode(): # Calls function with dnsmasq enable/start
                success = True
            else:
                print("ERROR: Failed to start Client mode services after stopping AP. Attempting to revert to AP mode...")
                start_ap_mode() # Try to go back to AP mode
        else:
            print("ERROR: Failed to stop AP mode services before starting client.")

        if success:
            print("Successfully switched to Client mode.")
            is_ap_mode_active = False
            btn_ap.setStyleSheet(button_style_sheet) # Inactive style
        else:
            print("Failed to switch to Client mode. Reverting selection visuals.")
            is_ap_mode_active = True # Stay in AP mode logically
            btn_ap.setStyleSheet(active_style_sheet_red) # Active style

# Exposure Button Handler
def on_exposure_button_clicked(button_widget, label):
    #Handles clicks on the bottom exposure time buttons.
    # Access global variables
    global active_exposure_button, picam2, general_settings, exposure_times, preview_config, current_manual_settings
    sender = button_widget
    if not sender:
        return # Exit if sender is somehow invalid

    if active_exposure_button == sender:
        # --- Revert to auto exposure ---
        sender.setStyleSheet(button_style_sheet) # Deactivate button visually
        active_exposure_button = None
        current_manual_settings = None # Clear stored manual settings
        print("Attempting to re-enable Auto Exposure...")
        try:
            # Try set_controls first (less disruptive)
            # Use general_settings which now doesn't explicitly set AnalogueGain
            picam2.set_controls(general_settings)
            print("Auto Exposure Re-enabled using set_controls")
        except Exception as e1:
            print(f"set_controls failed ({e1}), attempting configure...")
            try:
                # Fallback to reconfiguring if set_controls fails
                picam2.stop()
                # Create new config using general_settings (without AnalogueGain)
                new_config = picam2.create_preview_configuration(
                    main={"size": (960, 720)}, controls=general_settings
                )
                picam2.configure(new_config)
                picam2.start()
                print("Auto Exposure Re-enabled using configure")
            except Exception as e2:
                print(f"Error re-enabling auto exposure via configure: {e2}")
    else:
        # Set manual exposure
        # Deactivate previously active button (if any)
        if active_exposure_button:
            active_exposure_button.setStyleSheet(button_style_sheet)

        # Activate the newly clicked button
        sender.setStyleSheet(active_style_sheet_red) # Use red text style
        active_exposure_button = sender
        exposure_time = exposure_times[label]
        # Define the manual settings dictionary
        manual_settings = {
            "AnalogueGain": 1.0, # Explicitly set gain for manual mode
            "AeEnable": False, # Disable Auto Exposure
            "ExposureTime": exposure_time, # Set manual time
            "AwbEnable": True, # Keep Auto White Balance
            "AwbMode": controls.AwbModeEnum.Auto,
        }
        # Store the settings globally
        current_manual_settings = manual_settings
        print(f"Attempting to set Manual Exposure: {label} ({exposure_time} us)")
        try:
            # Try set_controls first
            picam2.set_controls(manual_settings)
            print(f"Manual Exposure Set using set_controls")
        except Exception as e1:
            print(f"set_controls failed ({e1}), attempting configure...")
            try:
                # Fallback to reconfiguring
                picam2.stop()
                new_config = picam2.create_preview_configuration(
                    main={"size": (960, 720)}, controls=manual_settings
                )
                picam2.configure(new_config)
                picam2.start()
                print(f"Manual Exposure Set using configure")
            except Exception as e2:
                 print(f"Error setting manual exposure via configure: {e2}")
# End on_exposure_button_clicked


def on_shutdown_button_clicked():
    #Handles clicks on the Shutdown ('I/O') button.
    global window
    print("Shutdown button clicked.")
    reply = QMessageBox.question(window,
                                 'Confirm Shutdown',
                                 'Are you sure you want to shut down the Raspberry Pi?',
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                 QMessageBox.StandardButton.No) # Default to No
    if reply == QMessageBox.StandardButton.Yes:
        print("User confirmed shutdown. Executing shutdown command...")
        command = ['sudo', '/sbin/shutdown', '-h', 'now']
        if not run_system_command(command):
            # Show error message if shutdown command fails
            QMessageBox.critical(window, "Shutdown Error", "Failed to execute shutdown command. Check logs or sudoers configuration.")
    else:
        print("Shutdown cancelled by user.")

# Preview Update Function (With Grid)
def update_preview(label):
    #Captures a frame, draws a grid overlay, and updates the preview label.
    try:
        # Capture frame
        array = picam2.capture_array("main")
        height, width, channels = array.shape
        bytesPerLine = 4 * width # RGBA8888 format
        qim = QImage(array.data, width, height, bytesPerLine, QImage.Format_RGBA8888)

        # Crop to square (720x720) from center width, top height
        crop_w = 720
        crop_h = 720
        crop_x = (width - crop_w) // 2
        crop_y = 0 # Crop from the top
        cropped_qim = qim.copy(crop_x, crop_y, crop_w, crop_h)
        pix = QPixmap.fromImage(cropped_qim)

        # Draw Grid Overlay using QPainter
        painter = QPainter(pix)
        pen_color = QColor(255, 255, 255, 100) # Semi-transparent white
        pen = QPen(pen_color)
        pen.setWidth(0) # Hairline width
        painter.setPen(pen)

        w = pix.width()
        h = pix.height()
        w_m1 = w - 1 # Max X coordinate
        h_m1 = h - 1 # Max Y coordinate

        # Golden Ratio lines calculation (relative to cropped image dimensions)
        base_center_y = h // 2
        base_gr_y1 = int(round(h * 0.381966))
        base_gr_y2 = int(round(h * 0.618034))
        center_x = w // 2
        gr_x1 = int(round(w * 0.381966))
        gr_x2 = int(round(w * 0.618034))

        # Adjust Y coordinates for the button bar height visually
        # Note: This shifts the grid *up* relative to the image content
        # to compensate for the space taken by the bottom button bar.
        y_shift = BUTTON_BAR_HEIGHT
        shifted_center_y = base_center_y - y_shift
        shifted_gr_y1 = base_gr_y1 - y_shift
        shifted_gr_y2 = base_gr_y2 - y_shift

        # Draw horizontal lines (shifted)
        painter.drawLine(0, shifted_center_y, w_m1, shifted_center_y)
        painter.drawLine(0, shifted_gr_y1, w_m1, shifted_gr_y1)
        painter.drawLine(0, shifted_gr_y2, w_m1, shifted_gr_y2)
        # Draw vertical lines (not shifted)
        painter.drawLine(center_x, 0, center_x, h_m1)
        painter.drawLine(gr_x1, 0, gr_x1, h_m1)
        painter.drawLine(gr_x2, 0, gr_x2, h_m1)

        painter.end()

        # Update the label
        label.setPixmap(pix)
    except Exception as e:
        print(f"Error updating preview: {e}")
        traceback.print_exc() # Print full traceback for debugging preview errors

# Image Saving Function
def save_image():
    # Captures and saves a full-resolution image.
    print("Saving image...")
    timestamp = time.strftime("%Y%m%d%H%M%S")
    filename = f"{timestamp}.jpg"
    save_dir = "/srv/DCIM/" # Target save directory

    try:
        # Ensure save directory exists
        os.makedirs(save_dir, exist_ok=True)
        filepath = os.path.join(save_dir, filename)

        # Configure for high-resolution still capture
        # Use sensor resolution for maximum quality
        # Important: Create a *copy* of current manual settings if active,
        # otherwise use default still config. Don't modify global dict here.
        if current_manual_settings:
             # Use manual settings for the capture, but ensure format/size are appropriate
             still_controls = current_manual_settings.copy()
             still_config = picam2.create_still_configuration(
                 main={"format": "XRGB8888", "size": picam2.sensor_resolution},
                 controls=still_controls
             )
             print("Using manual settings for still capture.")
        else:
             # Use default auto settings for capture
             still_config = picam2.create_still_configuration(
                 main={"format": "XRGB8888", "size": picam2.sensor_resolution}
                 # No explicit controls means it uses auto-exposure for the capture
             )
             print("Using auto settings for still capture.")

        # Set JPEG quality (optional, default is often reasonable)
        picam2.options['quality'] = 95 # 0-95, higher is better quality/larger file

        print(f"Attempting to save still to {filepath}...")
        # Use switch_mode_and_capture_file for efficient high-res capture
        job_maybe_dict = picam2.switch_mode_and_capture_file(still_config, filepath)
        print(f"Image save process initiated/completed for: {filepath}")

    except Exception as e:
        print(f"Error saving image: {e}")
        traceback.print_exc() # Print full traceback for debugging saving errors

# Self-Timer Callback Function
def delayed_capture_and_reset():
    # Called by QTimer after delay. Captures image if not cancelled.
    global is_timer_countdown_active, btn_timer, button_style_sheet
    capture_done = False
    if is_timer_countdown_active:
        # Timer completed normally
        print("Timer finished! Capturing image now...")
        save_image()
        capture_done = True # Mark capture as done
    else:
        # Timer was cancelled before completion
        print("Timer finished, but capture was cancelled by user.")

    # Reset timer state and button style regardless of capture
    is_timer_countdown_active = False
    if btn_timer: # Check if button widget exists
        btn_timer.setStyleSheet(button_style_sheet) # Use original base style

    # Re-apply manual exposure if needed *after* capture and timer reset
    if capture_done:
        reapply_manual_exposure_if_needed()

# gpiozero Button Handler
def handle_capture_press():
    #Callback function for gpiozero button press.
    global is_timer_countdown_active
    if is_timer_countdown_active:
        print("Timer countdown active, physical button press ignored.")
    else:
        print("Physical button pressed (gpiozero), saving image immediately.")
        save_image()
        # Re-apply manual exposure if it was set
        reapply_manual_exposure_if_needed()

# Define gpiozero Button Object
capture_button = None
try:
    # Initialize the button connected to the specified GPIO pin
    capture_button = Button(button_pin, pull_up=True, bounce_time=0.3) # Debounce time
    # Assign the handler function to the button's press event
    capture_button.when_pressed = handle_capture_press
    print(f"gpiozero button initialized for pin {button_pin}.")
except Exception as e:
    # Catch errors during button initialization (e.g., pin unavailable, library issues)
    print(f"!!!!!!!!!!\nERROR initializing gpiozero Button on pin {button_pin}: {e}\nPhysical button capture will NOT work.\n!!!!!!!!!!")
    traceback.print_exc()

# --- PyQt5 Application Setup ---
print("Setting up application...")
app = QApplication(sys.argv)
window = QWidget()
window.setStyleSheet("background-color: black;")
window.setFixedSize(720, 792) # Fixed size: 720 width, 720 preview + 2*36 button bars
window.setWindowFlags(Qt.FramelessWindowHint) # Remove window decorations

# Main vertical layout
main_layout = QVBoxLayout()
main_layout.setContentsMargins(0, 0, 0, 0) # No margins
main_layout.setSpacing(0) # No spacing between elements

# --- Top Button Bar GUI ---
top_button_layout = QHBoxLayout()
top_button_layout.setContentsMargins(0, 0, 0, 0)
top_button_layout.setSpacing(0)
# Create Buttons
btn_save = QPushButton("O") # Capture/Save button
btn_save.setFixedSize(72, 36)
btn_save.setStyleSheet(button_style_sheet) # Use original base style
btn_save.clicked.connect(on_save_button_clicked)

btn_timer = QPushButton("10s") # Self-timer button
btn_timer.setFixedSize(72, 36)
btn_timer.setStyleSheet(button_style_sheet) # Use original base style
btn_timer.clicked.connect(on_timer_button_clicked)

btn_ap = QPushButton("AP") # Access Point mode button
btn_ap.setFixedSize(72, 36)
btn_ap.setStyleSheet(button_style_sheet) # Use original base style
btn_ap.clicked.connect(on_ap_button_clicked)

btn_wifi = QPushButton("WiFi") # Master WiFi on/off button
btn_wifi.setFixedSize(72, 36)
# Set initial style based on startup check
if is_wifi_on:
    btn_wifi.setStyleSheet(active_style_sheet_red) # Active style
else:
    btn_wifi.setStyleSheet(button_style_sheet) # Inactive style
btn_wifi.clicked.connect(on_wifi_button_clicked)

btn_shutdown = QPushButton("I/O") # Shutdown button
btn_shutdown.setFixedSize(72, 36)
btn_shutdown.setStyleSheet(button_style_sheet) # Use original base style
btn_shutdown.clicked.connect(on_shutdown_button_clicked)

# Set initial enabled state for AP button based on wifi state
if not is_wifi_on:
    btn_ap.setEnabled(False)

# Add Widgets to TOP button layout: O | 10s | Stretch | WiFi | AP | I/O
top_button_layout.addWidget(btn_save)
top_button_layout.addWidget(btn_timer)
top_button_layout.addStretch(1) # Pushes right-side buttons to the right
top_button_layout.addWidget(btn_wifi)
top_button_layout.addWidget(btn_ap)
top_button_layout.addWidget(btn_shutdown)
# Removed battery_label addition

main_layout.addLayout(top_button_layout) # Add top bar to main layout

# --- Preview Label GUI ---
preview_label = QLabel() # Label to display the camera preview
preview_label.setFixedSize(720, 720) # Square preview area
preview_label.setStyleSheet("margin: 0px; padding: 0px; border: 0px;") # Ensure no extra space
main_layout.addWidget(preview_label) # Add preview label to main layout

# Bottom Button Bar GUI (Exposure Times)
bottom_button_layout = QHBoxLayout()
bottom_button_layout.setContentsMargins(0, 0, 0, 0)
bottom_button_layout.setSpacing(0)
# Create buttons for each exposure time
for label in exposure_times.keys():
    button = QPushButton(label)
    button.setFixedSize(72, 36)
    button.setStyleSheet(button_style_sheet) # Use original base style
    # Use lambda to pass button widget and label to the handler
    button.clicked.connect(lambda checked, b=button, l=label: on_exposure_button_clicked(b, l))
    bottom_button_layout.addWidget(button)
main_layout.addLayout(bottom_button_layout) # Add bottom bar to main layout

# Finalize Layout & Show Window
window.setLayout(main_layout)
window.showFullScreen() # Show the window in full screen mode

# Setup Preview Update Timer
preview_timer = QTimer() # Timer to refresh the preview label
preview_timer.timeout.connect(lambda: update_preview(preview_label)) # Connect timeout signal
print("Starting preview timer...")
preview_timer.start(33) # Update roughly 30 times per second (1000ms / 30fps ≈ 33ms)

# Define consolidated cleanup function
def proper_cleanup():
    # Stops timers, camera, and closes gpiozero button.
    print("Performing final cleanup...")
    if 'preview_timer' in globals() and preview_timer and preview_timer.isActive():
        preview_timer.stop()
        print("Preview timer stopped.")

    # Stop Picamera2 if it's running
    try:
        # Check if picam2 exists and is the correct type before trying to stop
        if 'picam2' in globals() and isinstance(picam2, Picamera2) and picam2.started:
            print("Stopping Picamera2...")
            picam2.stop()
            print("Picamera2 stopped.")
    except Exception as e:
        print(f"Error stopping camera during cleanup: {e}")

    # Close gpiozero button if it exists and is open
    try:
        if 'capture_button' in globals() and capture_button and not capture_button.closed:
            capture_button.close()
            print("gpiozero button closed.")
    except Exception as e:
        print(f"Error closing gpiozero button during cleanup: {e}")

    print("Application finished (physical cleanup).")

# Register the cleanup function to run on script exit
atexit.register(proper_cleanup)

# Run Application Event Loop
print("Starting application event loop...")
exit_code = 0
try:
    # Start the Qt event loop
    exit_code = app.exec_()
    print(f"Application event loop finished normally with exit code: {exit_code}")
except KeyboardInterrupt:
    # Handle Ctrl+C gracefully
    print("\nKeyboardInterrupt caught, exiting...")
    exit_code = 1 # Indicate exit via interrupt
except Exception as e:
    # Catch any other unhandled exceptions during the event loop
    print(f"\nUnhandled exception in Qt event loop: {e}")
    traceback.print_exc()
    exit_code = 2 # Indicate exit due to error
finally:
    # This block runs regardless of how the try block exits
    # Note: atexit cleanup runs *after* this finally block if sys.exit() is called
    print(f"Exiting script with code {exit_code}...")
    # Ensure sys.exit is called to trigger atexit handlers properly
    sys.exit(exit_code)

