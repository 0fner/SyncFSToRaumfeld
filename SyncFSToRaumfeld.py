#!/usr/bin/env python3

import sys
import time
import logging
import threading
from dataclasses import dataclass
from functools import wraps
import raumfeld
from fsapi import FSAPI


# ********** CONFIGURATION START **********
RADIO_HOST_IP = "192.168.0.70"
RADIO_PIN = 1234
RADIO_MODE = "AUX in"
RADIO_VOLUME = 20
LOG_LEVEL = logging.ERROR
STREAMER_ROOM_NAME = "Bed Room"
RADIO_TIMEOUT = 3 # [s]
SLEEP_TIME = 1 # [s]
# ********** CONFIGURATION END **********


# decorator to make function calls more reliable
def retry(_func=None, *, retries=5, sleep=0.5):
    def retry_decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    logging.warning("attempt %d: an exception occured: %s" % (attempt, str(e)))
                    time.sleep((sleep))
                    continue
            else:
                logging.error("too many failed attempts, giving up")
                raise Exception("too many failed attempts")
        return wrapper

    if _func is None:
        # decorator was called with arguments
        return retry_decorator
    else:
        # decorator was called without arguments
        return retry_decorator(_func)


@dataclass
class RadioState():
    volume: int
    mode: str
    power: bool
    mute: bool


# connect to Frontier Silicon radio
@retry
def connect_to_radio():
    logging.info("connecting to Frontier Silicon radio...")
    url = "http://" + RADIO_HOST_IP + ":80/device"
    radio = FSAPI(url, RADIO_PIN, RADIO_TIMEOUT)
    logging.info("connected to " + radio.friendly_name)
    return radio


# read radio state
@retry
def get_radio_state(radio):
    state = RadioState(radio.volume, radio.mode, radio.power, radio.mute)
    assert isinstance(state.volume, int), "did not receive a valid value for: volume"
    assert isinstance(state.mode, str), "did not receive a valid value for: mode"
    assert isinstance(state.power, bool), "did not receive a valid value for: power"
    assert isinstance(state.mute, bool), "did not receive a valid value for: mute"
    logging.info("current radio state: power=%d, mode=%s, volume=%d, mute=%d" % (state.power, state.mode, state.volume, state.mute))
    return state


# set radio state
@retry
def set_radio_state(radio, state):
    logging.info("setting new radio state: power=%d, mode=%s, volume=%d, mute=%d" % (state.power, state.mode, state.volume, state.mute))
    # note: setting the mode also switches on the radio; a power off command will only be effective if there is a time delay between setting the mode and the power off command
    radio.mode = state.mode
    time.sleep(0.3)
    radio.volume = state.volume
    time.sleep(0.3)
    radio.mute = state.mute
    time.sleep(0.3)
    radio.power = state.power


# connect to Teufel Raumfeld host
@retry
def connect_to_raumfeld():
    logging.info("connecting to Teufel Streamer...")
    raumfeld.setLogging(LOG_LEVEL)
    raumfeld.init()
    if raumfeld.hostBaseURL == "http://hostip:47365":
        raise Exception("could not connect to Teufel Raumfeld host")
    logging.info("connected to " + raumfeld.hostBaseURL)


# callback function which is called whenever the Raumfeld zone/room/device configuration changes
def get_streamer():
    global streamer, streamer_lock

    streamer_zone = raumfeld.getZoneWithRoomName(STREAMER_ROOM_NAME)
    streamer_lock.acquire()
    if len(streamer_zone) > 1:
        logging.error("multiple Teufel Streamer devices were found, check the Streamer name")
        streamer = None
    elif len(streamer_zone) == 0:
        logging.error("no Teufel Streamer device was found, check the Streamer name")
        streamer = None
    else:
        streamer = streamer_zone[0]
        logging.info("connected to zone " + streamer.Name)
    streamer_lock.release()


# configure logging
logging.basicConfig(format='%(asctime)s %(message)s', level=LOG_LEVEL)


# connect to Raumfeld system and register callback for Raumfeld configuration changes
streamer_lock = threading.Lock()
streamer = None
connect_to_raumfeld()
get_streamer()
raumfeld.registerChangeCallback(get_streamer)

# connect to radio and define radio state when streamer is active
radio = connect_to_radio()
radio_streaming_state = RadioState(volume=RADIO_VOLUME, mode=RADIO_MODE, power=True, mute=False)


# main loop
streamer_active = False
previous_radio_state = None
while True:

    # get current state of Teufel Streamer
    streamer_lock.acquire()
    try:
        rf_state = str(streamer.transport_info["CurrentTransportState"]) # possible states: STOPPED/PLAYING/PAUSED_PLAYBACK/NO_MEDIA_PRESENT/TRANSITIONING
    except Exception as err:
        logging.warning("lost connection to Teufel Streamer: " + str(err))
        rf_state = None
    streamer_lock.release()


    # set correct radio mode
    if rf_state == "PLAYING" and not streamer_active:
        # switch on radio
        logging.info("Teufel Streamer switched on, changing radio input to streamer device")

        # set the streaming flag at the beginning to avoid retrieving an invalid radio state on the next round if an error occurs
        streamer_active = True

        try:
            # check if session is still active
            if radio.volume == None:
                radio = connect_to_radio()
            previous_radio_state = get_radio_state(radio)
            set_radio_state(radio, radio_streaming_state)
            time.sleep(1)
        except Exception as err:
            logging.error(str(err))

    elif (rf_state == "STOPPED" or rf_state == "PAUSED_PLAYBACK" or rf_state == "NO_MEDIA_PRESENT") and streamer_active:
        # restore previous radio mode
        logging.info("Teufel Streamer switched off, restoring previous radio state")

        try:
            # check if session is still active
            if radio.volume == None:
                radio = connect_to_radio()
            if previous_radio_state:
                current_radio_state = get_radio_state(radio)
                # make sure radio stays off if it has been switched off manually while streaming
                if not current_radio_state.power:
                    previous_radio_state.power = False
                set_radio_state(radio, previous_radio_state)
                time.sleep(1)

            # set the streaming flag at the end, so that set_radio_state is called again in the next round if an error occurs
            streamer_active = False
        except Exception as err:
            logging.error(str(err))


    # slow down while loop
    time.sleep(SLEEP_TIME)
