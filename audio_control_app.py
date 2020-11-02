#!/usr/bin/python3.7 

from nearfield.nfccontroller import NFCController
from vlccontrol.vlccontroller import VLCController
from socketmonitor.socketmonitor import music_socket_monitor_worker
from plexinterface.plexinterface import PlexInterface

import argparse

from multiprocessing import Process, Queue
from enum import Enum, auto
import logging
import sys
from time import sleep
import json

logging.basicConfig(format='%(filename)s.%(lineno)d:%(levelname)s:%(message)s',
                    level=logging.DEBUG)

class VirtualJukebox(object):
    """App driving the physical hardware/music interface.

    Manages playing audio via VLC, driven by tags read from an NFCController

    """


    class State(Enum):
        WAITING = auto(),
        PLAYING = auto() 

    class PlayType(Enum):
        NONE = auto(),  # Not playing anything.  Should be the case when self._state = WAITING
        NFC = auto(),   # Playing audio from the NFC reader
        STREAM = auto() # Playing audio from a streaming source (e.g. Plex)

    def __init__(self, nfcQueue, socketQueue, log_to_file=False): 

        self._logger = logging.getLogger('nfc_audio')
        
        if log_to_file: 
            self._logger.setLevel(logging.DEBUG)

            fh = logging.FileHandler('/tmp/audio.log')
            fh.setLevel(logging.DEBUG)
            self._logger.addHandler(fh)
            self._logger.debug('Initialized logger')
        
        self._nfc = NFCController(message_queue = nfcQueue)
        self._vlc = None
        self._state = VirtualJukebox.State.WAITING
        self._playType = VirtualJukebox.PlayType.NONE
        self._currentlyPlayingURI = None

        self._nfcQueue = nfcQueue
        self._socketQueue = socketQueue

        self._plex = PlexInterface()

    def _initInterfaces(self):
        self._vlc = VLCController()
        self._plex.connect()  # This will fail if the server isn't up.  Should do so gracefully, and reconnect when needed

    def process_queue_message(self, message):
        """Takes in a message from the data sources, and triggers audio events as appropriate

        Args:
            message (str): JSON string describing an event from the source, and necessary audio data
        """

        if not self._vlc:  # initInterfaces hasn't yet been called
            self._initInterfaces()
        
        # Convert the string to a dict, and validate the content
        try:
            messageDict = json.loads(message)
        except json.decoder.JSONDecodeError:
            self._logger.error('Invalid JSON string on queue: [{0}]'.format(message))
            return
        
        if messageDict['event'] == 'start' and not self._vlc:
            self._vlc = VLCController()

        if messageDict['source'] == 'nfc':
            self._process_queue_message__nfc(messageDict)
        elif messageDict['source'] == 'plex':
            self._process_queue_message__plex(messageDict)
        elif messageDict['source'] == 'remote':
            self._process_queue_message__remote(messageDict)
        else:
            self._logger.error('Invalid source: [{0}]'.format(message))


    def _process_queue_message__nfc(self, messageDict):

        tagInfo = messageDict['data']
        self._logger.debug('Procesing NFC message: {0}'.format(messageDict))


        if messageDict['event'] == 'start':
            
            if tagInfo['uri'] == self._currentlyPlayingURI:
                self._vlc._media_list_player.play()
                self._state = VirtualJukebox.State.PLAYING
                self._playType = VirtualJukebox.PlayType.NFC
                return

            self._vlc._media_list_player.stop()
            self._logger.debug('Building medialist')
            ml = self._vlc.build_medialist_from_uri(tagInfo['uri'])
            self._vlc._media_list_player.set_media_list(ml)
            self._vlc._media_list_player.play()

            self._logger.debug('Setting state to PLAYING')
            self._state = VirtualJukebox.State.PLAYING
            self._playType = VirtualJukebox.PlayType.NFC
            self._currentlyPlayingURI = tagInfo['uri']

        # We only request a stop command if we're playing audio triggered by the NFC device
        if messageDict['event'] == 'stop' and self._playType == VirtualJukebox.PlayType.NFC:
            self._logger.debug('Tag is no longer present.  Stopping music')
            self._logger.debug('Setting state to WAITING')
            self._vlc._media_list_player.pause()
            self._state = VirtualJukebox.State.WAITING
            self._playType = VirtualJukebox.PlayType.NONE


    def _process_queue_message__plex(self, messageDict):

        dataPayload = messageDict['data']
        event = messageDict['event']

        if event == 'start':
            streamURLs = self._plex.getStreamURLsForAlbum(dataPayload['album'])

            self._logger.debug('List of URLS: {0}'.format(streamURLs))
            self._vlc.stop()
            mediaList = self._vlc.convert_filepaths_to_medialist(streamURLs)
            self._vlc._media_list_player.set_media_list(mediaList)  # TODO: don't call this directly
            
            self._playType == VirtualJukebox.PlayType.STREAM
            self._vlc.play()


    def _process_queue_message__remote(self, messageDict):

        dataPayload = messageDict['data']
        event = messageDict['event']

        if event == 'pause':
            self._logger.debug('Received pause from remote')
            if self._state == VirtualJukebox.State.PLAYING:
                self._vlc._media_list_player.pause()
                self._state = VirtualJukebox.State.WAITING
                self._playType = VirtualJukebox.PlayType.NONE

            elif self._state == VirtualJukebox.State.WAITING:
                self._vlc._media_list_player.play()
                self._state = VirtualJukebox.State.PLAYING
                self._playType = VirtualJukebox.PlayType.STREAM

        elif event == 'forward':
            self._logger.debug('Received forward from remote')
            self._vlc._media_list_player.next()

        elif event == 'previous':
            self._logger.debug('Received back from remote')
            self._vlc._media_list_player.previous()

    def run(self):

        try:
            while True:

                # This pings the NFC, updates its internal tag states, and will push a message onto 
                # the NFCQueue
                self._nfc.sense_for_target_tag()

                # Poll the queues to see if there's anything in there waiting for me
                for q in [self._nfcQueue, self._socketQueue]:
                    if not q.empty():
                        message = q.get(block=False, timeout=0.01)
                        if not message:
                            continue

                        self._logger.debug('Message from queue: {0}'.format(message) )
                        self.process_queue_message(message)


            sleep(0.1)  # There's no real need to poll the NFC device at an incredibly high frequency

        except KeyboardInterrupt:
            self._logger.debug('Ctrl-c interrupt:  Exiting application')
            sys.exit(0)
        



def parse_arguments(argv):

    parser = argparse.ArgumentParser()
    parser.add_argument( '-s', '--sleep', dest='sleep_time',
                         type=int, default=0, 
                         help='Sets how long to sleep on script start')

    parser.add_argument( '-f', '--filelog', dest='filelog',
                         type=bool, default=False,
                         help='If present, will send logs to /tmp/audio.log')

    return parser.parse_args(argv)



if __name__ == '__main__':
    
    args = parse_arguments(sys.argv[1:])
    sleep(args.sleep_time) 

    host = ''  # Since we're listening, we don't need a port. 
    port = 32413

    nfcQueue = Queue()
    socketQueue = Queue()

    socket_process = Process(target = music_socket_monitor_worker, args=(host, port, socketQueue,))
    socket_process.start()

    try:
        app = VirtualJukebox(nfcQueue, socketQueue, log_to_file=args.filelog)
        app.run()
    except (KeyboardInterrupt, SystemExit):
        pass

    logging.debug('Joining the socket process')
    socket_process.join()
