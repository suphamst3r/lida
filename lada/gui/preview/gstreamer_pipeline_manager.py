# SPDX-FileCopyrightText: Lada Authors
# SPDX-License-Identifier: AGPL-3.0

import logging
import pathlib
import sys
import threading
from enum import Enum
from time import sleep

from gi.repository import GObject, GLib, Gst, GstApp, Gdk, Gio

from lada import LOG_LEVEL
from lada.gui.frame_restorer_provider import FrameRestorerProvider
from lada.gui.preview.gstreamer_pipeline_appsrc import FrameRestorerAppSrc
from lada.lib import VideoMetadata, audio_utils

logger = logging.getLogger(__name__)
logging.basicConfig(level=LOG_LEVEL)

class PipelineState(Enum):
    PLAYING = 1
    PAUSED = 2

class PipelineManager(GObject.Object):
    def __init__(self, frame_restorer_provider: FrameRestorerProvider, buffer_queue_min_thresh_time, buffer_queue_max_thresh_time, muted: bool):
        super().__init__()
        self.frame_restorer_app_src: FrameRestorerAppSrc | None = None
        self.video_metadata: VideoMetadata | None = None
        self.frame_restorer_provider: FrameRestorerProvider = frame_restorer_provider
        self.buffer_queue_min_thresh_time = buffer_queue_min_thresh_time
        self.buffer_queue_max_thresh_time = buffer_queue_max_thresh_time
        self._paintable: Gdk.Paintable | None
        self._state: PipelineState = PipelineState.PAUSED
        self.has_audio: bool = False
        self._muted: bool = muted

        self.audio_filesrc: Gst.Element | None = None
        self.audio_uridecodebin: Gst.Element | None = None
        self.audio_volume = None
        self.pipeline: Gst.Pipeline = Gst.Pipeline.new()
        self.video_buffer_queue: Gst.Queue | None = None
        self.audio_buffer_queue: Gst.Queue | None = None
        self.pipeline_audio_elements = []

    @GObject.Property(type=Gdk.Paintable)
    def paintable(self):
        return self._paintable

    @paintable.setter
    def paintable(self, value: Gdk.Paintable):
        self._paintable = value

    @GObject.Property()
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        self._state = value

    @GObject.Property()
    def muted(self):
        return self._muted

    @muted.setter
    def muted(self, value):
        self._muted = value
        if self.audio_volume:
            self.audio_volume.set_property("mute", value)

    @GObject.Signal(name="waiting-for-data")
    def buffer_queue_underrun(self, waiting_for_data: bool):
        pass

    @GObject.Signal(name="eos")
    def eos(self):
        pass

    @GObject.Signal(name="paintable-size-changed")
    def paintable_size_changed(self):
        pass

    def play(self):
        self.pipeline.set_state(Gst.State.PLAYING)

    def pause(self):
        self.pipeline.set_state(Gst.State.PAUSED)

    def get_position_ns(self):
        res, position = self.pipeline.query_position(Gst.Format.TIME)
        valid_position = res and position >= 0
        return position if valid_position else None

    def on_bus_msg(self, _, msg: Gst.Message):
        if msg.type == Gst.MessageType.EOS:
            self.state = PipelineState.PAUSED
            GLib.idle_add(lambda: self.emit("eos"))
        elif msg.type == Gst.MessageType.ERROR:
            (err, _) = msg.parse_error()
            logger.error(f"Error from {msg.src.get_path_string()}: {err}")
            # If error is from audio elements and we have audio enabled, disable audio
            if self.has_audio and "tsdemux" in msg.src.get_path_string():
                logger.warning("Disabling audio due to tsDemux error")
                self.has_audio = False
                self.pipeline_remove_audio()
        elif msg.type == Gst.MessageType.STATE_CHANGED:
            if msg.src == self.pipeline:
                old_state, new_state, pending_state = msg.parse_state_changed()
                if old_state == Gst.State.PAUSED and new_state == Gst.State.PLAYING:
                    self.state = PipelineState.PLAYING
                elif old_state == Gst.State.PLAYING and new_state == Gst.State.PAUSED:
                    self.state = PipelineState.PAUSED
        elif msg.type == Gst.MessageType.STREAM_STATUS:
            pass
        else:
            # print("other message", msg.type)
            pass
        return True

    def init_pipeline(self, video_metadata: VideoMetadata):
        if self.video_metadata:
            self.adjust_pipeline_with_new_source_file(video_metadata)
        else:
            self.video_metadata = video_metadata
            self.has_audio = audio_utils.get_audio_codec(self.video_metadata.video_file) is not None

            bus = self.pipeline.get_bus()
            bus.add_watch(GLib.PRIORITY_DEFAULT, self.on_bus_msg)

            self.pipeline_add_video()
            if self.has_audio:
                self.pipeline_add_audio()

    def close_video_file(self):
        if self.audio_volume:
            self.audio_volume.set_property("mute", True)
        self.pipeline.set_state(Gst.State.NULL)
        while not self.pipeline.get_state(Gst.CLOCK_TIME_NONE)[1] == Gst.State.NULL:
            sleep(0.05)
        self.frame_restorer_app_src.stop()
        # self.pipeline.get_bus().remove_watch()

    def seek_async(self, seek_position_ns):
        #  seek_simple() is blocking. As we're stopping/starting our appsrc on seek this could potentially take a few seconds.
        # As this method is used from the UI it could introduce freezes so let's run this in another thread.
        def do_seek():
            # TODO: Evaluate if this statement about pausing before seeking is actually true
            # Pausing before seek seems to fix an issue where calling seek_simple() never returns.
            # I did not notice it on smaller/shorter files but on long files (>3h) I could reproduce this issue pretty consistently.
            # Shouldn't be necessary and I don't understand how it helps but apparently it does.
            self.pipeline.set_state(Gst.State.PAUSED)
            self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, seek_position_ns)
            logger.debug("returned from pipeline.seek_simple()")
            self.pipeline.set_state(Gst.State.PLAYING)

        seek_thread = threading.Thread(target=do_seek, daemon=True)
        seek_thread.start()

    def pipeline_add_audio(self):
        # Check if mpegtsdemux is available, otherwise fall back to uridecodebin
        audio_mpegtsdemux = Gst.ElementFactory.make('mpegtsdemux')
        if audio_mpegtsdemux is None:
            logger.warning("mpegtsdemux not available, falling back to uridecodebin")
            self.pipeline_add_audio_fallback()
            return

        audio_filesrc = Gst.ElementFactory.make('filesrc')
        if audio_filesrc is None:
            logger.error("Failed to create filesrc element")
            self.has_audio = False
            return
        audio_filesrc.set_property('location', self.video_metadata.video_file)
        self.pipeline.add(audio_filesrc)
        self.pipeline_audio_elements.append(audio_filesrc)

        self.pipeline.add(audio_mpegtsdemux)
        self.pipeline_audio_elements.append(audio_mpegtsdemux)

        audio_decodebin = Gst.ElementFactory.make('decodebin')
        if audio_decodebin is None:
            logger.error("Failed to create decodebin element")
            self.has_audio = False
            return

        def on_pad_added(decodebin, decoder_src_pad, audio_queue):
            caps = decoder_src_pad.get_current_caps()
            if not caps:
                caps = decoder_src_pad.query_caps()
            gststruct = caps.get_structure(0)
            gstname = gststruct.get_name()
            if gstname.startswith("audio"):
                sink_pad = audio_queue.get_static_pad("sink")
                decoder_src_pad.link(sink_pad)

        audio_decodebin.connect("pad-added", on_pad_added, None)
        self.pipeline.add(audio_decodebin)
        self.pipeline_audio_elements.append(audio_decodebin)

        audio_queue = Gst.ElementFactory.make('queue')
        if audio_queue is None:
            logger.error("Failed to create queue element")
            self.has_audio = False
            return
        audio_queue.set_property('max-size-bytes', 0)
        audio_queue.set_property('max-size-buffers', 0)
        audio_queue.set_property('max-size-time', self.buffer_queue_max_thresh_time * Gst.SECOND)  # ns
        audio_queue.set_property('min-threshold-time', self.buffer_queue_min_thresh_time * Gst.SECOND)
        self.pipeline.add(audio_queue)
        self.pipeline_audio_elements.append(audio_queue)

        audio_audioconvert = Gst.ElementFactory.make('audioconvert')
        if audio_audioconvert is None:
            logger.error("Failed to create audioconvert element")
            self.has_audio = False
            return
        self.pipeline.add(audio_audioconvert)
        self.pipeline_audio_elements.append(audio_audioconvert)

        audio_audioresample = Gst.ElementFactory.make('audioresample')
        if audio_audioresample is None:
            logger.error("Failed to create audioresample element")
            self.has_audio = False
            return
        self.pipeline.add(audio_audioresample)
        self.pipeline_audio_elements.append(audio_audioresample)

        audio_volume = Gst.ElementFactory.make('volume')
        if audio_volume is None:
            logger.error("Failed to create volume element")
            self.has_audio = False
            return
        audio_volume.set_property("mute", self._muted)
        self.pipeline.add(audio_volume)
        self.pipeline_audio_elements.append(audio_volume)

        audio_sink = Gst.ElementFactory.make('autoaudiosink')
        if audio_sink is None:
            logger.error("Failed to create autoaudiosink element")
            self.has_audio = False
            return
        self.pipeline.add(audio_sink)
        self.pipeline_audio_elements.append(audio_sink)

        # Link the elements
        audio_filesrc.link(audio_mpegtsdemux)
        # audio_mpegtsdemux will dynamically link to audio_decodebin via pad-added
        audio_mpegtsdemux.connect("pad-added", lambda demux, pad: pad.link(audio_decodebin.get_static_pad("sink")) if pad.get_current_caps().get_structure(0).get_name().startswith("audio") else None)
        # note that we cannot link decodebin directly to audio_queue as pads are dynamically added and not available at this point
        # see on_pad_added()
        audio_queue.link(audio_audioconvert)
        audio_audioconvert.link(audio_audioresample)
        audio_audioresample.link(audio_volume)
        audio_volume.link(audio_sink)

        self.audio_filesrc = audio_filesrc
        self.audio_volume = audio_volume
        self.audio_buffer_queue = audio_queue

    def pipeline_add_audio_fallback(self):
        audio_queue = Gst.ElementFactory.make('queue')
        if audio_queue is None:
            logger.error("Failed to create queue element")
            self.has_audio = False
            return
        audio_queue.set_property('max-size-bytes', 0)
        audio_queue.set_property('max-size-buffers', 0)
        audio_queue.set_property('max-size-time', self.buffer_queue_max_thresh_time * Gst.SECOND)  # ns
        audio_queue.set_property('min-threshold-time', self.buffer_queue_min_thresh_time * Gst.SECOND)
        self.pipeline.add(audio_queue)
        self.pipeline_audio_elements.append(audio_queue)

        audio_uridecodebin = Gst.ElementFactory.make('uridecodebin')
        if audio_uridecodebin is None:
            logger.error("Failed to create uridecodebin element")
            self.has_audio = False
            return
        audio_uridecodebin.set_property('uri', self.path_to_gst_uri(self.video_metadata.video_file))

        def on_pad_added(decodebin, decoder_src_pad, audio_queue):
            caps = decoder_src_pad.get_current_caps()
            if not caps:
                caps = decoder_src_pad.query_caps()
            gststruct = caps.get_structure(0)
            gstname = gststruct.get_name()
            if gstname.startswith("audio"):
                sink_pad = audio_queue.get_static_pad("sink")
                decoder_src_pad.link(sink_pad)

        audio_uridecodebin.connect("pad-added", on_pad_added, audio_queue)
        self.pipeline.add(audio_uridecodebin)
        self.pipeline_audio_elements.append(audio_uridecodebin)

        audio_audioconvert = Gst.ElementFactory.make('audioconvert')
        if audio_audioconvert is None:
            logger.error("Failed to create audioconvert element")
            self.has_audio = False
            return
        self.pipeline.add(audio_audioconvert)
        self.pipeline_audio_elements.append(audio_audioconvert)

        audio_audioresample = Gst.ElementFactory.make('audioresample')
        if audio_audioresample is None:
            logger.error("Failed to create audioresample element")
            self.has_audio = False
            return
        self.pipeline.add(audio_audioresample)
        self.pipeline_audio_elements.append(audio_audioresample)

        audio_volume = Gst.ElementFactory.make('volume')
        if audio_volume is None:
            logger.error("Failed to create volume element")
            self.has_audio = False
            return
        audio_volume.set_property("mute", self._muted)
        self.pipeline.add(audio_volume)
        self.pipeline_audio_elements.append(audio_volume)

        audio_sink = Gst.ElementFactory.make('autoaudiosink')
        if audio_sink is None:
            logger.error("Failed to create autoaudiosink element")
            self.has_audio = False
            return
        self.pipeline.add(audio_sink)
        self.pipeline_audio_elements.append(audio_sink)

        # note that we cannot link decodebin directly to audio_queue as pads are dynamically added and not available at this point
        # see on_pad_added()
        audio_queue.link(audio_audioconvert)
        audio_audioconvert.link(audio_audioresample)
        audio_audioresample.link(audio_volume)
        audio_volume.link(audio_sink)

        self.audio_uridecodebin = audio_uridecodebin
        self.audio_volume = audio_volume
        self.audio_buffer_queue = audio_queue

    def pipeline_add_video(self):
        self.frame_restorer_app_src = FrameRestorerAppSrc(self.video_metadata, self.frame_restorer_provider, lambda: GLib.idle_add(lambda: self.emit("waiting-for-data", False)))
        appsrc = self.frame_restorer_app_src.appsrc
        self.pipeline.add(appsrc)

        buffer_queue = Gst.ElementFactory.make('queue', None)
        buffer_queue.set_property('max-size-bytes', 0)
        buffer_queue.set_property('max-size-buffers', 0)
        buffer_queue.set_property('max-size-time', self.buffer_queue_max_thresh_time * Gst.SECOND)  # ns
        buffer_queue.set_property('min-threshold-time', self.buffer_queue_min_thresh_time * Gst.SECOND)

        buffer_queue.connect("underrun", lambda queue: GLib.idle_add(lambda: self.emit("waiting-for-data", True)))
        buffer_queue.connect("overrun", lambda queue: GLib.idle_add(lambda: self.emit("waiting-for-data", False)))
        self.pipeline.add(buffer_queue)

        gtksink = Gst.ElementFactory.make('gtk4paintablesink', None)
        paintable: Gdk.Paintable = gtksink.get_property('paintable')
        # TODO: workaround for #62. On Windows using Nvidia GPU and OpenGL for the paintable when it's available causes messed up colors.
        #  I could not reproduce this on a VM without a Nvidia card.
        if paintable.props.gl_context and sys.platform != 'win32':
            video_sink = Gst.ElementFactory.make('glsinkbin', None)
            video_sink.set_property('sink', gtksink)
        else:
            video_sink = Gst.Bin.new()
            convert = Gst.ElementFactory.make('videoconvert', None)
            video_sink.add(convert)
            video_sink.add(gtksink)
            convert.link(gtksink)
            video_sink.add_pad(Gst.GhostPad.new('sink', convert.get_static_pad('sink')))
        self.pipeline.add(video_sink)

        appsrc.link(buffer_queue)
        buffer_queue.link(video_sink)

        self.video_buffer_queue = buffer_queue
        self.paintable = paintable
        self.paintable.connect("invalidate-size", lambda obj: GLib.idle_add(lambda: self.emit("paintable-size-changed")))

    def pipeline_remove_audio(self):
        for audio_element in self.pipeline_audio_elements:
            audio_element.set_state(Gst.State.NULL)
            self.pipeline.remove(audio_element)
        self.audio_filesrc = None
        self.audio_uridecodebin = None
        self.audio_volume = None
        self.audio_buffer_queue = None

    def adjust_pipeline_with_new_source_file(self, video_metadata: VideoMetadata):
        self.video_metadata = video_metadata
        self.frame_restorer_app_src.reinit(self.video_metadata)
        audio_pipeline_already_added = self.has_audio
        self.has_audio = audio_utils.get_audio_codec(self.video_metadata.video_file) is not None
        if self.has_audio:
            if audio_pipeline_already_added:
                if self.audio_filesrc:
                    self.audio_filesrc.set_property('location', self.video_metadata.video_file)
                elif self.audio_uridecodebin:
                    self.audio_uridecodebin.set_property('uri', self.path_to_gst_uri(self.video_metadata.video_file))
            else:
                self.pipeline_add_audio()
        else:
            self.pipeline_remove_audio()

    def reinit_appsrc(self):
        self.frame_restorer_app_src.reinit(self.video_metadata)

        # seeking flush to flush pipeline / clean out buffers
        res, position = self.pipeline.query_position(Gst.Format.TIME)
        if res and position >= 0:
            self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, position)

    def update_gst_buffers(self, buffer_queue_min_thresh_time, buffer_queue_max_thresh_time):
        self.video_buffer_queue.set_property('max-size-time', buffer_queue_max_thresh_time * Gst.SECOND)
        self.video_buffer_queue.set_property('min-threshold-time', buffer_queue_min_thresh_time * Gst.SECOND)
        if self.has_audio and self.audio_buffer_queue:
            self.audio_buffer_queue.set_property('max-size-time', buffer_queue_max_thresh_time * Gst.SECOND)
            self.audio_buffer_queue.set_property('min-threshold-time', buffer_queue_min_thresh_time * Gst.SECOND)

    def path_to_gst_uri(self, path: str):
        # On Windows Gst expects 4-slash URI format syntax. So \\1.2.3.4\share\file.mp4 needs to end up as file:////1.2.3.4/share/file.mp4
        # pathlib:Path::as_uri returns regular 2-slash format so we use Gio:File::get_uri instead
        abs_path = str(pathlib.Path(path).resolve())
        file = Gio.File.new_for_path(abs_path)
        return file.get_uri()