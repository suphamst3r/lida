import logging
import os
import pathlib
import tempfile
import threading
import time
import traceback
import shutil

from gi.repository import Gtk, GObject, Gio, Adw, GLib

from lada import LOG_LEVEL
from lada import _
from lada.gui import utils
from lada.gui.config.config import Config
from lada.gui.config.no_gpu_banner import NoGpuBanner
from lada.gui.export import export_utils
from lada.gui.export.export_item_data import ExportItemData, ExportItemDataProgress, ExportItemState
from lada.gui.export.export_multiple_files_page import ExportMultipleFilesPage
from lada.gui.export.export_single_file_page import ExportSingleFileStatusPage
from lada.gui.export.export_utils import ResumeInformation
from lada.gui.export.spinner_button import SpinnerButton
from lada.gui.frame_restorer_provider import FrameRestorerOptions, FRAME_RESTORER_PROVIDER
from lada.lib import audio_utils, video_utils

here = pathlib.Path(__file__).parent.resolve()

logger = logging.getLogger(__name__)
logging.basicConfig(level=LOG_LEVEL)

@Gtk.Template(string=utils.translate_ui_xml(here / 'export_view.ui'))
class ExportView(Gtk.Widget):
    __gtype_name__ = 'ExportView'

    single_file_page: ExportSingleFileStatusPage = Gtk.Template.Child()
    multiple_files_page: ExportMultipleFilesPage = Gtk.Template.Child()
    button_start_export: Gtk.Button = Gtk.Template.Child()
    button_cancel_export: SpinnerButton = Gtk.Template.Child()
    button_resume_export: SpinnerButton = Gtk.Template.Child()
    button_pause_export: SpinnerButton = Gtk.Template.Child()
    stack: Gtk.Stack = Gtk.Template.Child()
    view_switcher: Adw.ViewSwitcher = Gtk.Template.Child()
    config_sidebar = Gtk.Template.Child()
    button_add_files: Gtk.Button = Gtk.Template.Child()
    banner_no_gpu: NoGpuBanner = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._view_stack: Adw.ViewStack | None = None
        self._config: Config | None = None
        self.in_progress_idx: int | None = None
        self.single_file = True
        self.stop_requested = False
        self.pause_requested = False
        self.resume_info: ResumeInformation | None = None
        self.video_writer: video_utils.VideoWriter | None = None
        self.progress_calculator: export_utils.ProgressCalculator | None = None

        self.connect("video-export-finished", self.on_video_export_finished)
        self.connect("video-export-failed", self.on_video_export_failed)
        self.connect("video-export-progress", self.on_video_export_progress)
        self.connect("video-export-resumed", self.on_video_export_resumed)
        self.connect("video-export-paused", self.on_video_export_paused)
        self.connect("video-export-stopped", self.on_video_export_stopped)

        self.model =  Gio.ListStore(item_type=ExportItemData)
        self.multiple_files_page.bind(self.model)

        def on_files_added(obj, files):
            self.button_add_files.set_sensitive(True)
            self.add_files(files)
        self.connect("files-added", on_files_added)

        self.single_file_page.connect("start-export-requested", lambda page, button: self.on_button_start_export_clicked(button))
        self.single_file_page.connect("stop-export-requested", self.on_button_cancel_export_clicked)
        self.single_file_page.connect("pause-export-requested", self.on_button_pause_export_clicked)
        self.single_file_page.connect("resume-export-requested", self.on_button_resume_export_clicked)

        self.multiple_files_page.connect("show-error-requested", self.on_show_error_requested)
        self.multiple_files_page.connect("remove-item-requested", self.on_remove_item_requested)

        drop_target = utils.create_video_files_drop_target(lambda files: self.emit("files-added", files))
        self.add_controller(drop_target)

    @GObject.Property(type=Config)
    def config(self):
        return self._config

    @config.setter
    def config(self, value):
        self._config = value
        self._config.connect("notify::export-directory", self.on_config_changed)
        self._config.connect("notify::file-name-pattern", self.on_config_changed)
        self.set_restore_button_label()

    @GObject.Property(type=Adw.ViewStack)
    def view_stack(self):
        return self._view_stack

    @view_stack.setter
    def view_stack(self, value: Adw.ViewStack):
        self._view_stack = value
        def on_visible_child_name_changed(object, spec):
            visible_child_name = object.get_property(spec.name)
            if visible_child_name == "export":
                self.config_sidebar.init_sidebar_from_config(self._config)
        self._view_stack.connect("notify::visible-child-name", on_visible_child_name_changed)

    def add_files(self, added_files: list[Gio.File]):
        assert len(added_files) > 0

        for original_file in added_files:
            if any([original_file.get_path() == item.original_file.get_path() for item in self.model]):
                # duplicate
                continue
            if self._config.export_directory:
                restored_file = self.get_restored_file_path(original_file, self._config.export_directory)
            else:
                # We don't know the output directory yet. This guess needs to be updated after the user set one via FilePicker
                restored_file = self.get_restored_file_path(original_file, added_files[0].get_parent().get_path())
            export_item = ExportItemData(original_file, restored_file)
            self.model.append(export_item)

        self.single_file = len(self.model) == 1

        if self.single_file:
            self.stack.set_visible_child_name("single-file")
            self.single_file_page.on_add_file(self.model[0])
        else:
            self.stack.set_visible_child_name("multiple-files")
            self.update_export_buttons()

    def update_export_buttons(self):
        if self.single_file:
            return
        count_queued_items = sum([item.state == ExportItemState.QUEUED for item in self.model])
        is_in_progress = self.in_progress_idx is not None
        is_paused = self.resume_info is not None
        is_any_queued_items = count_queued_items > 0
        self.button_start_export.set_visible(not is_in_progress and is_any_queued_items)
        self.button_pause_export.set_visible(is_in_progress and not is_paused)
        self.button_resume_export.set_visible(is_paused)
        self.button_cancel_export.set_visible(is_in_progress)

    @GObject.Signal(name="video-export-finished")
    def video_export_finished_signal(self):
        pass

    @GObject.Signal(name="video-export-failed", arg_types=(GObject.TYPE_STRING,))
    def video_export_failed_signal(self, error_message: str):
        pass

    @GObject.Signal(name="video-export-paused",)
    def video_export_paused_signal(self):
        pass

    @GObject.Signal(name="video-export-resumed",)
    def video_export_resumed_signal(self):
        pass

    @GObject.Signal(name="video-export-stopped",)
    def video_export_stopped_signal(self):
        pass

    @GObject.Signal(name="video-export-progress", arg_types=(ExportItemDataProgress,))
    def video_export_progress_signal(self, progress):
        pass

    @GObject.Signal(name="video-export-requested")
    def video_export_requested_signal(self, save_file: Gio.File):
        pass

    @GObject.Signal(name="files-added", arg_types=(GObject.TYPE_PYOBJECT,))
    def files_opened_signal(self, files: list[Gio.File]):
        pass

    @Gtk.Template.Callback()
    def on_button_start_export_clicked(self, start_export_button: Gtk.Button):
        if self._config.export_directory:
            item = self.model[self.get_next_queued_item_idx()]
            self.emit("video-export-requested", item.restored_file)
        else:
            start_export_button.set_sensitive(False)
            dismissed_callback = lambda *args: start_export_button.set_sensitive(True)
            self.show_export_dialog(dismissed_callback)

    @Gtk.Template.Callback()
    def button_add_files_callback(self, button_clicked):
        self.button_add_files.set_sensitive(False)
        callback = lambda files: self.emit("files-added", files)
        dismissed_callback = lambda *args: self.button_add_files.set_sensitive(True)
        utils.show_open_files_dialog(callback, dismissed_callback)

    @Gtk.Template.Callback()
    def on_button_cancel_export_clicked(self, button_clicked):
        self.stop_requested = True
        self.button_pause_export.set_sensitive(False)
        self.button_cancel_export.set_sensitive(False)
        self.button_cancel_export.set_spinner_visible(True)

    @Gtk.Template.Callback()
    def on_button_pause_export_clicked(self, button_clicked):
        assert self.resume_info is None
        self.pause_requested = True
        self.button_pause_export.set_sensitive(False)
        self.button_pause_export.set_spinner_visible(True)
        self.button_cancel_export.set_sensitive(False)

    @Gtk.Template.Callback()
    def on_button_resume_export_clicked(self, button_clicked):
        assert self.resume_info is not None
        self.button_resume_export.set_sensitive(False)
        self.button_resume_export.set_spinner_visible(True)
        self.button_cancel_export.set_sensitive(False)

        self.pause_requested = False
        assert self.in_progress_idx is not None
        item = self.model[self.in_progress_idx]
        self._start_export(item.original_file, item.restored_file)

    def on_show_error_requested(self, obj, idx):
        model_item = self.model[idx]
        export_utils.open_error_dialog(self, model_item.original_file.get_basename(), model_item.error_details)

    def on_remove_item_requested(self, obj, idx):
        self.model.remove(idx)
        self.update_export_buttons()

    def on_config_changed(self, *args):
        if self._config.export_directory:
            for idx, model_item in enumerate(self.model):
                if model_item.state == ExportItemState.QUEUED:
                    restored_file = self.get_restored_file_path(model_item.original_file, self._config.export_directory)
                    model_item.restored_file = restored_file
                    self.multiple_files_page.on_restored_file_changed(idx, restored_file)
        self.set_restore_button_label()

    def set_restore_button_label(self):
        label = _("Restore") if self._config.export_directory else _("Restore…")
        self.single_file_page.set_button_start_restore_label(label)
        self.button_start_export.set_label(label)

    def get_next_queued_item_idx(self) -> int | None:
        for idx, item in enumerate(self.model):
            if item.state == ExportItemState.QUEUED:
                return idx
        return None

    def continue_next_file(self):
        next_idx = self.get_next_queued_item_idx()
        if next_idx is None:
            # done, all queued items processed
            self.view_switcher.set_sensitive(True)
            self.config_sidebar.set_property("disabled", False)
            self.in_progress_idx = None
            self.update_export_buttons()
        else:
            # continue, queued items remaining
            self._start_export(self.model[next_idx].original_file, self.model[next_idx].restored_file)

    def show_video_export_started(self, save_file: Gio.File):
        self.view_switcher.set_sensitive(False)
        self.config_sidebar.set_property("disabled", True)

        idx = self.get_next_queued_item_idx()
        if idx is None:
            return

        self.in_progress_idx = idx
        self.update_export_buttons()

        model_item = self.model[idx]
        model_item.state = ExportItemState.PROCESSING

        if self.single_file:
            self.single_file_page.show_video_export_started(save_file)
        self.multiple_files_page.show_video_export_started(idx)

    def on_video_export_finished(self, obj):
        assert self.in_progress_idx is not None

        model_item = self.model[self.in_progress_idx]
        model_item.progress.complete()
        model_item.state = ExportItemState.FINISHED

        if self.single_file:
            self.single_file_page.on_video_export_finished()
        self.multiple_files_page.on_video_export_finished(self.in_progress_idx)

        self.continue_next_file()

    def on_video_export_progress(self, obj, progress: ExportItemDataProgress):
        if self.in_progress_idx is None:
            return

        model_item = self.model[self.in_progress_idx]
        model_item.progress = progress

        if self.single_file:
            self.single_file_page.on_video_export_progress(progress)
        self.multiple_files_page.on_video_export_progress(self.in_progress_idx, progress)

    def on_video_export_stopped(self, obj):
        assert self.in_progress_idx is not None

        model_item = self.model[self.in_progress_idx]
        model_item.state = ExportItemState.QUEUED
        model_item.progress = ExportItemDataProgress()

        if self.single_file:
            self.single_file_page.on_video_export_stopped()
        self.multiple_files_page.on_video_export_stopped(self.in_progress_idx)

        self.in_progress_idx = None
        self.stop_requested = False
        self.update_export_buttons()
        self.view_switcher.set_sensitive(True)
        self.config_sidebar.set_property("disabled", False)
        self.button_start_export.set_sensitive(True)
        self.button_cancel_export.set_sensitive(True)
        self.button_cancel_export.set_spinner_visible(False)
        self.button_pause_export.set_sensitive(True)

    def on_video_export_paused(self, obj):
        assert self.in_progress_idx is not None

        model_item = self.model[self.in_progress_idx]
        model_item.state = ExportItemState.PAUSED

        if self.single_file:
            self.single_file_page.on_video_export_paused()
        self.multiple_files_page.on_video_export_paused(self.in_progress_idx)

        self.update_export_buttons()
        self.button_pause_export.set_sensitive(True)
        self.button_pause_export.set_spinner_visible(False)
        self.button_cancel_export.set_sensitive(True)
        self.pause_requested = False

    def on_video_export_resumed(self, obj):
        assert self.in_progress_idx is not None

        model_item = self.model[self.in_progress_idx]
        assert model_item.state == ExportItemState.PAUSED
        model_item.state = ExportItemState.PROCESSING

        if self.single_file:
            self.single_file_page.on_video_export_resumed()
        self.multiple_files_page.on_video_export_resumed(self.in_progress_idx)

        self.update_export_buttons()
        self.button_resume_export.set_sensitive(True)
        self.button_resume_export.set_spinner_visible(False)
        self.button_cancel_export.set_sensitive(True)

    def on_video_export_failed(self, obj, error_message):
        assert self.in_progress_idx is not None

        model_item = self.model[self.in_progress_idx]
        model_item.state = ExportItemState.FAILED
        model_item.error_details = error_message

        if self.single_file:
            self.single_file_page.on_video_export_failed()
        self.multiple_files_page.on_video_export_failed(self.in_progress_idx)

        export_utils.open_error_dialog(self, model_item.original_file.get_basename(), error_message)

        self.continue_next_file()

    def start_export(self, restore_directory_or_file: Gio.File):
        # Update initial guessed output restore directory/file now that the user has provided it via file/dir picker dialog
        if not self._config.export_directory:
            restored_files: list[Gio.File] = []
            if self.single_file:
                assert len(self.model) == 1
                restored_file = restore_directory_or_file
                model_item = self.model[0]
                model_item.restored_file = restored_file
                restored_files.append(restored_file)
            else:
                assert os.path.isdir(restore_directory_or_file.get_path())
                restore_directory = restore_directory_or_file
                for idx, model_item in enumerate(self.model):
                    restored_file = self.get_restored_file_path(model_item.original_file, restore_directory.get_path())
                    model_item.restored_file = restored_file
                    restored_files.append(restored_file)
            self.multiple_files_page.on_video_export_started(restored_files)

        item = self.model[self.get_next_queued_item_idx()]
        self._start_export(item.original_file, item.restored_file)

    def _start_export(self, source_file: Gio.File, restore_file: Gio.File):
        assert os.path.isfile(source_file.get_path())
        if not self.resume_info:
            self.show_video_export_started(restore_file)
        def run_export():
            frame_restorer_options = FrameRestorerOptions(self._config.mosaic_restoration_model, self._config.mosaic_detection_model, video_utils.get_video_meta_data(source_file.get_path()), self._config.device, self._config.max_clip_duration, False, False)
            video_metadata = frame_restorer_options.video_metadata
            frame_restorer_provider = FRAME_RESTORER_PROVIDER
            frame_restorer_provider.init(frame_restorer_options)
            frame_restorer = frame_restorer_provider.get()
            restore_file_path = restore_file.get_path()

            # how often (in frames) to update progress/estimate. Smaller -> more responsive UI
            progress_update_step_size = 25
            # estimate audio bytes by probing source file audio bitrate (bps -> bytes)
            audio_estimated_bytes = 0
            try:
                src_path = source_file.get_path()
                # ffprobe: get audio stream bit_rate if available
                import subprocess
                cmd = ['ffprobe', '-v', 'quiet', '-select_streams', 'a', '-show_entries', 'stream=bit_rate', '-of', 'default=nw=1:nk=1', src_path]
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                out, err = p.communicate(timeout=5)
                if p.returncode == 0 and out:
                    try:
                        bitrate_str = out.decode().strip().splitlines()[0]
                        bitrate = int(bitrate_str)
                        # bytes = bits/8 * duration
                        audio_estimated_bytes = int((bitrate / 8.0) * float(video_metadata.duration))
                    except Exception:
                        audio_estimated_bytes = 0
            except Exception:
                audio_estimated_bytes = 0
            success = True
            base_tmp_dir = tempfile.gettempdir()
            try:
                if self._config and getattr(self._config, 'temp_dir', None):
                    base_tmp_dir = self._config.temp_dir
            except Exception:
                pass
            video_tmp_file_output_path = os.path.join(base_tmp_dir, f"{os.path.basename(os.path.splitext(restore_file_path)[0])}.tmp{os.path.splitext(restore_file_path)[1]}")
            try:
                if self.resume_info:
                    start_ns = self.resume_info.get_resume_timestamp_ns()
                    start_frame_num = self.resume_info.frame_num
                    logger.info(f"Resume requested: Starting FrameRestorer at timestamp {start_ns}ns")
                else:
                    start_ns = 0
                    start_frame_num = 0
                    self.video_writer = video_utils.VideoWriter(
                        video_tmp_file_output_path, video_metadata.video_width,
                        video_metadata.video_height, video_metadata.video_fps_exact,
                        self._config.export_codec, time_base=video_metadata.time_base,
                        crf=self._config.export_crf, custom_encoder_options=self._config.custom_ffmpeg_encoder_options)
                    self.progress_calculator = export_utils.ProgressCalculator(video_metadata)

                frame_restorer.start(start_ns=start_ns)

                duration_start = time.time()
                for frame_num, elem in enumerate(frame_restorer, start=start_frame_num):
                    if self.stop_requested:
                        success = False
                        logger.warning("Stop requested: Stopping FrameRestorer")
                        break
                    if elem is None:
                        success = False
                        logger.error("Error on export: frame restorer stopped prematurely")
                        break

                    (restored_frame, restored_frame_pts) = elem
                    if self.resume_info:
                        if restored_frame_pts <= self.resume_info.frame_pts:
                            logging.debug("Received frame earlier than resume position, skipping frame...")
                            continue
                        else:
                            logger.debug("Received first frame after resume position, successful resume.")
                            self.resume_info = None
                            GLib.idle_add(lambda: self.emit('video-export-resumed'))
                    self.video_writer.write(restored_frame, restored_frame_pts, bgr2rgb=True)

                    duration_end = time.time()
                    duration = duration_end - duration_start
                    duration_start = duration_end
                    self.progress_calculator.update(duration)
                    # update progress and estimated size periodically
                    if frame_num % progress_update_step_size == 0:
                        try:
                            progress = self.progress_calculator.get_progress()
                            # estimate final bytes based on current temp file size and processed fraction
                            try:
                                if os.path.exists(video_tmp_file_output_path) and progress.fraction > 0:
                                    tmp_size = os.path.getsize(video_tmp_file_output_path)
                                    # scale by inverse of fraction (avoid div by zero)
                                    video_estimated_total = int(tmp_size / max(progress.fraction, 1e-6))
                                else:
                                    video_estimated_total = 0
                            except Exception:
                                video_estimated_total = 0
                            estimated_total = int(video_estimated_total + (audio_estimated_bytes or 0))
                            progress.estimated_bytes = estimated_total
                            GLib.idle_add(lambda p=progress: self.emit('video-export-progress', p))
                        except Exception:
                            # fall back to emitting basic progress
                            GLib.idle_add(lambda: self.emit('video-export-progress', self.progress_calculator.get_progress()))

                    if self.pause_requested:
                        logger.info("Pause requested: Pausing FrameRestorer")
                        self.resume_info = ResumeInformation(restored_frame_pts, video_metadata.time_base, frame_num)
                        break

            except Exception as e:
                success = False
                err_msg = "".join(traceback.format_exception_only(e))
                GLib.idle_add(lambda: self.emit('video-export-failed', err_msg))
            finally:
                if not self.pause_requested:
                    self.video_writer.release()
                frame_restorer.stop()

            if self.pause_requested:
                GLib.idle_add(lambda: self.emit('video-export-paused'))
            else:
                if success:
                    try:
                        frame_rate_mode = getattr(self._config, 'export_frame_rate_mode', 'auto') if self._config is not None else 'auto'
                    except Exception:
                        frame_rate_mode = 'auto'
                    subtitle_path = None
                    subtitle_mode = 'passthrough'
                    try:
                        if self._config:
                            subtitle_path = getattr(self._config, 'export_subtitle_path', None)
                            subtitle_mode = getattr(self._config, 'export_subtitle_mode', 'passthrough')
                    except Exception:
                        pass
                    audio_utils.combine_audio_video_files(video_metadata, video_tmp_file_output_path, restore_file_path, frame_rate_mode=frame_rate_mode, subtitle_path=subtitle_path, subtitle_mode=subtitle_mode)
                    def on_success():
                        progress = self.progress_calculator.get_progress()
                        # final size: prefer the combined restore_file if present
                        try:
                            if os.path.exists(restore_file_path):
                                final_size = os.path.getsize(restore_file_path)
                            else:
                                final_size = 0
                        except Exception:
                            final_size = 0
                        progress.estimated_bytes = final_size
                        progress.complete()
                        self.emit('video-export-progress', progress)
                        self.emit('video-export-finished')
                        # perform post-export actions configured by the user
                        try:
                            self.perform_post_export_actions()
                        except Exception as e:
                            logger.exception(f"Error performing post-export actions: {e}")
                    GLib.idle_add(on_success)
                else:
                    if os.path.exists(video_tmp_file_output_path):
                        os.remove(video_tmp_file_output_path)
            if self.stop_requested:
                GLib.idle_add(lambda: self.emit('video-export-stopped'))

        exporter_thread = threading.Thread(target=run_export)
        exporter_thread.start()

    def show_export_dialog(self, dismissed_callback):
        def on_dialog_result(dialog, result):
            try:
                if self.single_file:
                    selected = dialog.save_finish(result)
                else:
                    selected = dialog.select_folder_finish(result)
                if selected is not None:
                    self.emit("video-export-requested",selected)
            except GLib.Error as error:
                if error.message == "Dismissed by user":
                    dismissed_callback()
                    logger.debug("FileDialog cancelled: Dismissed by user")
                else:
                    logger.error(f"Error opening file: {error.message}")
                    raise error

        if self.single_file:
            file_dialog = Gtk.FileDialog()
            video_file_filter = Gtk.FileFilter()
            video_file_filter.add_mime_type("video/*")
            file_dialog.set_default_filter(video_file_filter)
            file_dialog.set_title(_("Save restored video file"))
            initial_restored_file = self.model[0].restored_file
            file_dialog.set_initial_folder(initial_restored_file.get_parent())
            file_dialog.set_initial_name(initial_restored_file.get_basename())
            file_dialog.save(callback=on_dialog_result)
        else:
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title(_("Save restored video files"))
            first_original_file = self.model[0].original_file
            file_dialog.set_initial_folder(first_original_file.get_parent())
            file_dialog.select_folder(callback=on_dialog_result)

    def get_restored_file_path(self, original_file: Gio.File, output_dir: str) -> Gio.File:
        orig_file_name = os.path.splitext(original_file.get_basename())[0]
        restored_file_name = self._config.file_name_pattern.replace("{orig_file_name}", orig_file_name)
        return Gio.File.new_build_filenamev([output_dir, restored_file_name])

    def close(self):
        self.stop_requested = True

    def perform_post_export_actions(self):
        if not self._config:
            return
        import platform
        import subprocess
        # run commands
        cmds = self._config.post_export_commands
        if cmds:
            for cmd in str(cmds).split(';'):
                cmd = cmd.strip()
                if not cmd:
                    continue
                try:
                    subprocess.Popen(cmd, shell=True)
                except Exception:
                    logger.exception(f"Failed to run post-export command: {cmd}")
        # play sound
        sound = self._config.post_export_sound
        if sound:
            try:
                # Try simple cross-platform approaches
                if platform.system() == 'Windows':
                    # use powershell PlaySound if available
                    subprocess.Popen(["powershell", "-c", f"(New-Object Media.SoundPlayer '{sound}').PlaySync();"], shell=False)
                else:
                    # try aplay or paplay or afplay
                    for player in ("paplay", "aplay", "afplay", "ffplay"):
                        try:
                            if shutil.which(player):
                                if player == 'ffplay':
                                    subprocess.Popen([player, '-nodisp', '-autoexit', sound])
                                else:
                                    subprocess.Popen([player, sound])
                                break
                        except Exception:
                            continue
            except Exception:
                logger.exception("Failed to play post-export sound")
        # shutdown
        if self._config.post_export_shutdown:
            try:
                # If user requested confirmation show a dialog and abort shutdown if they cancel
                confirm = True
                try:
                    confirm_pref = getattr(self._config, 'post_export_confirm_shutdown', True)
                except Exception:
                    confirm_pref = True
                if confirm_pref:
                    countdown_seconds = 10
                    md = Gtk.Dialog(transient_for=self.get_root(), modal=True)
                    md.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
                    md.set_default_size(360, 120)
                    box = md.get_content_area()
                    label = Gtk.Label(label=_('The system will shutdown in {n} seconds.').format(n=countdown_seconds))
                    label.set_wrap(True)
                    box.append(label)
                    md.show()

                    data = {
                        'remaining': countdown_seconds,
                        'cancelled': False,
                    }

                    def on_dialog_response(dialog, response):
                        if response == Gtk.ResponseType.CANCEL:
                            data['cancelled'] = True
                        try:
                            dialog.destroy()
                        except Exception:
                            pass

                    md.connect('response', on_dialog_response)

                    def tick():
                        if data['cancelled']:
                            return False
                        data['remaining'] -= 1
                        if data['remaining'] <= 0:
                            try:
                                md.destroy()
                            except Exception:
                                pass
                            # proceed with shutdown
                            return False
                        # update label
                        label.set_text(_('The system will shutdown in {n} seconds.').format(n=data['remaining']))
                        return True

                    # update once per second
                    GLib.timeout_add_seconds(1, tick)
                    # Wait until dialog is destroyed or cancelled: poll every 0.1s
                    while True:
                        if data['cancelled']:
                            confirm = False
                            break
                        # if md was destroyed, we can assume countdown finished
                        try:
                            if not md.get_visible():
                                break
                        except Exception:
                            break
                        time.sleep(0.1)
                if not confirm:
                    logger.info('User cancelled post-export shutdown')
                else:
                    if platform.system() == 'Windows':
                        subprocess.Popen(["shutdown", "/s", "/t", "10"])  # 10s delay
                    else:
                        subprocess.Popen(["shutdown", "-h", "now"])  # may require sudo
            except Exception:
                logger.exception("Failed to initiate shutdown")
        # close application
        if self._config.post_export_close:
            try:
                # close top-level window
                GLib.idle_add(lambda: self.get_root().close())
            except Exception:
                try:
                    GLib.idle_add(lambda: Gtk.main_quit())
                except Exception:
                    pass
