# Copyright (c) 2014 Shotgun Software Inc.
# 
# CONFIDENTIAL AND PROPRIETARY
# 
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit 
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your 
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights 
# not expressly granted therein are reserved by Shotgun Software Inc.

"""
Flame Shot Exporter.

This app takes a sequence in flame and generates lots of Shotgun related content.
It is similar to the Hiero exporter. The following items are generated:

- New Shots in Shotgun, with tasks
- Cut information in Shotgun
- New versions (with uploaded quicktimes) for all segments
- Plates on disk for each segment
- Batch files for each shot
- Clip xml files for shots and clips.

The exporter is effectively a wrapper around the flame custom export process 
with some bindings to Shotgun.

This app implements two different sets of callbacks - both utilizing the same 
configuration and essentially parts of the same workflow (this is why they are not
split across two different apps).

- The flame Shot export runs via a context menu item on the sequence right-click menu
- A flare / batch mode render hook allows Shotgun to intercept the rendering process and
  ask the user if they want to submit to shotgun review whenever they render out in flame. 

"""

import uuid
import os
import re
import sgtk
import datetime
import pprint

from sgtk import TankError
from sgtk.platform import Application
    

class FlameExport(Application):
    """
    Export functionality to automate and streamline content export out of Flame.
    """
    
    def init_app(self):
        """
        Called as the application is being initialized.
        """
        self.log_debug("%s: Initializing" % self)
                
        # shot metadata. As the exporter steps through its various callbacks,
        # this data structure will be populated and used to pass information
        # down between methods.
        self._shots = {}
        
        # create a submit helper
        # because parts of this app runs on the farm, which doesn't have a UI,
        # there are two distinct modules on disk, one which is QT dependent and
        # one which isn't.
        tk_flame_export_no_ui = self.import_module("tk_flame_export_no_ui")
        self._sg_submit_helper = tk_flame_export_no_ui.ShotgunSubmitter()
        
        # batch render tracking - when doing a batch render, 
        # this is used to indicate that the user wants to send the render to review.
        self._send_batch_render_to_review = False
        
        # Shot export user UI input
        self._user_comments = ""
        self._video_preset = None
        
        # flag to indicate that something was actually submitted by the export process
        self._reached_post_asset_phase = False
        
        # register our desired interaction with flame hooks
        # set up callbacks for the engine to trigger 
        # when this profile is being triggered
        menu_caption = self.get_setting("menu_name")        
        callbacks = {}
        callbacks["preCustomExport"] = self.pre_custom_export
        callbacks["preExportSequence"] = self.pre_export_sequence
        callbacks["preExportAsset"] = self.pre_export_asset
        callbacks["postExportAsset"] = self.post_export_asset
        callbacks["postCustomExport"] = self.do_submission_and_summary
        self.engine.register_export_hook(menu_caption, callbacks)
        
        # also register this app so that it runs after export in batch mode / flare
        batch_callbacks = {}
        batch_callbacks["batchExportEnd"] = self.post_batch_render_sg_process
        batch_callbacks["batchExportBegin"] = self.pre_batch_render_checks
        self.engine.register_batch_hook(batch_callbacks)


    ##############################################################################################################
    # Flame shot export integration

    def pre_custom_export(self, session_id, info):
        """
        Hook called before a custom export begins. The export will be blocked
        until this function returns. This can be used to fill information that would
        have normally been extracted from the export window.
        
        :param info: Dictionary with info about the export. Contains the keys
                     - destinationHost: Host name where the exported files will be written to.
                     - destinationPath: Export path root.
                     - presetPath: Path to the preset used for the export.
                     - abort: Pass True back to flame if you want to abort
                     - abortMessage: Abort message to feed back to client
        """
        from PySide import QtGui
        
        # reset export session data
        self._shots = {}
        self._reached_post_asset_phase = False
        
        # get video preset names from config
        video_preset_names = [preset["name"] for preset in self.get_setting("plate_presets")]
        
        # pop up a UI asking the user for description
        tk_flame_export = self.import_module("tk_flame_export")  
        tk_flame_export_no_ui = self.import_module("tk_flame_export_no_ui")
                      
        (return_code, widget) = self.engine.show_modal("Export Shots",
                                                       self,
                                                       tk_flame_export.SubmitDialog, 
                                                       video_preset_names)
        
        if return_code == QtGui.QDialog.Rejected:
            # user pressed cancel
            info["abort"] = True
            info["abortMessage"] = "User cancelled the operation."
                   
        else:
            # get comments from user
            self._user_comments = widget.get_comments()
            self._video_preset = widget.get_video_preset()
            
            # populate the host to use for the export. Currently hard coded to local
            info["destinationHost"] = self.engine.get_server_hostname()
            
            # let the export root path align with the primary project root
            info["destinationPath"] = self.sgtk.project_path
            
            # pick up the xml export profile from the configuration
            export_preset = tk_flame_export_no_ui.ExportPreset()
            info["presetPath"] = export_preset.get_xml_path(self._video_preset)    
            self.log_debug("%s: Starting custom export session with preset '%s'" % (self, info["presetPath"]))
        
        
    def get_plate_template_for_preset(self, plate_preset):
        """
        Helper method. Returns the plate template for a given a configuration preset.
        
        :param plate_preset: preset name, as defined in the app settings (e.g. 10 bit DPX)
        :returns: template associated with this plate preset in the configuration.
        """
        template = None
        for preset in self.get_setting("plate_presets"):
            if preset["name"] == plate_preset:
                plate_template_name = preset["template"]
                template = self.get_template_by_name(plate_template_name) 
        if template is None:
            raise TankError("Cannot find preset '%s' in configuration!" % preset)
        return template


    def pre_export_sequence(self, session_id, info):
        """
        Called from the flame hooks before export.
        This is the time to set up the structure in Shotgun.
        
        :param session_id: String which identifies which export session is being referred to

        :param info: Information about the export. Contains the keys      
                     - destinationHost: Host name where the exported files will be written to.
                     - destinationPath: Export path root.
                     - sequenceName: Name of the exported sequence.
                     - shotNames: Tuple of all shot names in the exported sequence. 
                                  Multiple segments could have the same shot name.
                     - abort: Hook can set this to True if the export sequence process should
                              be aborted. If other sequences are exported in the same export session
                              they will still be exported even if this export sequence is aborted.
                     - abortMessage: Error message to be displayed to the user when the export sequence
                                     process has been aborted.
        """
        sequence_name = info["sequenceName"]
        shot_names = info["shotNames"]
        
        if len(shot_names) == 0:
            from PySide import QtGui     
            QtGui.QMessageBox.warning(None,
                                      "Please name your shots!",
                                      "The Shotgun integration requires you to name your shots. Please go back to "
                                      "the time line and ensure that all clips have been given shot names before "
                                      "proceeding!")
            info["abort"] = True
            info["abortMessage"] = "Cannot export due to missing shot names."
            return
        
        # process a sequence and some shots:
        # create entities in shotgun, create folders on disk and compute shot contexts.
        sequence_data = self._sg_submit_helper.create_shotgun_structure(sequence_name, shot_names)
        
        # add it to the full dictionary of things to export
        self._shots.update(sequence_data)

    
    def pre_export_asset(self, session_id, info):
        """
        Called when an item is about to be exported and a path needs to be computed.
        
        This will take the parameters from flame, push them through the toolkit template
        system and then return a path to flame that flame will be using for the export.
        
 
        :param session_id: String which identifies which export session is being referred to.
                           This parameter makes it possible to distinguish between different 
                           export sessions running if this is needed (typically only needed for
                           expert use cases).

        :param info: Dictionary with a number of parameters:
        
           destinationHost: Host name where the exported files will be written to.
           destinationPath: Export path root.
           namePattern:     List of optional naming tokens.
           resolvedPath:    Full file pattern that will be exported with all the tokens resolved.
           assetName:       Name of the exported asset.
           sequenceName:    Name of the sequence the asset is part of.
           shotName:        Name of the shot the asset is part of.
           assetType:       Type of exported asset. ( 'video', 'audio', 'batch', 'openClip', 'batchOpenClip' )
           width:           Frame width of the exported asset.
           height:          Frame height of the exported asset.
           aspectRatio:     Frame aspect ratio of the exported asset.
           depth:           Frame depth of the exported asset. ( '8-bits', '10-bits', '12-bits', '16 fp' )
           scanFormat:      Scan format of the exported asset. ( 'FIELD_1', 'FIELD_2', 'PROGRESSIVE' )
           fps:             Frame rate of exported asset.
           sequenceFps:     Frame rate of the sequence the asset is part of.
           sourceIn:        Source in point in frame and asset frame rate.
           sourceOut:       Source out point in frame and asset frame rate.
           recordIn:        Record in point in frame and sequence frame rate.
           recordOut:       Record out point in frame and sequence frame rate.
           track:           ID of the sequence's track that contains the asset.
           trackName:       Name of the sequence's track that contains the asset.
           segmentIndex:    Asset index (1 based) in the track.
           versionName:     Current version name of export (Empty if unversioned).
           versionNumber:   Current version number of export (0 if unversioned).        
        """
        
        asset_type = info["assetType"]
        asset_name = info["assetName"]
        shot_name = info["shotName"]
        sequence_name = info["sequenceName"]

        if asset_type not in ["video", "batch", "batchOpenClip", "openClip"]:
            # the review system ignores any other assets. The export profiles are defined
            # in the app's settings hook, so technically there shouldn't be any other items
            # generated - but just in case there are (because of customizations), we'll simply
            # ignore these.
            return
        
        # first check that the clip has a shot name - otherwise things won't work!
        if shot_name == "":
            from PySide import QtGui
            QtGui.QMessageBox.warning(None,
                                      "Missing shot name!",
                                      ("The clip '%s' does not have a shot name and therefore cannot be exported. "
                                      "Please ensure that all shots you wish to exports "
                                      "have been named. " % asset_name) )
            
            # TODO: send the clip to the trash for now. no way to abort at this point
            # but we don't have enough information to be able to proceed at this point either
            info["resolvedPath"] = "flame_trash/unnamed_shot_%s" % uuid.uuid4().hex
            
            # TODO: can we avoid this export altogether?
            return
        
        # first, calculate cut data fields
        if asset_type == "video":
            self._shots[sequence_name][shot_name].update_new_cut_info(int(info["recordIn"]), int(info["recordOut"]))
                
        # get the appropriate file system template
        if asset_type == "video":
            # exported plates or video
            template = self.get_plate_template_for_preset(self._video_preset)
            
        elif asset_type == "batch":
            # batch file
            template = self.get_template("batch_template")
            
        elif asset_type == "batchOpenClip":
            # shot level open scene clip xml
            template = self.get_template("shot_clip_template")            

        elif asset_type == "openClip":
            # segment level open scene clip xml
            template = self.get_template("segment_clip_template")            
        
        self.log_debug("Attempting to resolve template %s..." % template)
        
        # resolve the template via the context
        context = self._shots[sequence_name][shot_name].context

        # resolve the fields out of the context
        self.log_debug("Resolving template %s using context %s" % (template, context))
        fields = context.as_template_fields(template)
        self.log_debug("Resolved context based fields: %s" % fields)
        
        if asset_type == "video":
            # handle the flame sequence token - it will come in as "[1001-1100]"
            re_match = re.search("(\[[0-9]+-[0-9]+\])\.", info["resolvedPath"])
            if not re_match:
                raise TankError("Cannot find frame number token in export data!")
            fields["SEQ"] = re_match.group(1)

        # create some fields based on the info in the info params                
        if "versionNumber" in info:
            fields["version"] = int(info["versionNumber"])
        
        fields["segment_name"] = asset_name
            
        if "width" in info:
            fields["width"] = int(info["width"])

        if "height" in info:
            fields["height"] = int(info["height"])
        
        # populate the time field metadata
        now = datetime.datetime.now()
        fields["YYYY"] = now.year
        fields["MM"] = now.month
        fields["DD"] = now.day
        fields["hh"] = now.hour
        fields["mm"] = now.minute
        fields["ss"] = now.second
        
        try:
            full_path = template.apply_fields(fields)
        except Exception, e:
            raise TankError("Could not resolve a file system path " 
                            "from template %s and fields %s: %s" % (template, fields, e))
        
        self.log_debug("Resolved %s -> %s" % (fields, full_path))
        
        # chop off the root of the path - the resolvedPath should be local to the destinationPath
        local_path = full_path[len(info["destinationPath"])+1:]
        
        self.log_debug("Chopping off root path %s -> %s" % (full_path, local_path))
        
        # pass an updated path back to the flame. This ensures that all the 
        # character substitutions etc are handled according to the toolkit logic 
        info["resolvedPath"] = local_path        
        
    def post_export_asset(self, session_id, info):
        """
        Called when an item has been exported.
        
        :param session_id: String which identifies which export session is being referred to.
                           This parameter makes it possible to distinguish between different 
                           export sessions running if this is needed (typically only needed for
                           expert use cases).

        :param info: Dictionary with a number of parameters:
        
           destinationHost: Host name where the exported files will be written to.
           destinationPath: Export path root.
           namePattern:     List of optional naming tokens.
           resolvedPath:    Full file pattern that will be exported with all the tokens resolved.
           name:            Name of the exported asset.
           sequenceName:    Name of the sequence the asset is part of.
           shotName:        Name of the shot the asset is part of.
           assetType:       Type of exported asset. ( 'video', 'audio', 'batch', 'openClip', 'batchOpenClip' )
           isBackground:    True if the export of the asset happened in the background.
           backgroundJobId: Id of the background job given by the backburner manager upon submission. 
                            Empty if job is done in foreground.
           width:           Frame width of the exported asset.
           height:          Frame height of the exported asset.
           aspectRatio:     Frame aspect ratio of the exported asset.
           depth:           Frame depth of the exported asset. ( '8-bits', '10-bits', '12-bits', '16 fp' )
           scanFormat:      Scan format of the exported asset. ( 'FIELD_1', 'FIELD_2', 'PROGRESSIVE' )
           fps:             Frame rate of exported asset.
           sequenceFps:     Frame rate of the sequence the asset is part of.
           sourceIn:        Source in point in frame and asset frame rate.
           sourceOut:       Source out point in frame and asset frame rate.
           recordIn:        Record in point in frame and sequence frame rate.
           recordOut:       Record out point in frame and sequence frame rate.
           track:           ID of the sequence's track that contains the asset.
           trackName:       Name of the sequence's track that contains the asset.
           segmentIndex:    Asset index (1 based) in the track.       
           versionName:     Current version name of export (Empty if unversioned).
           versionNumber:   Current version number of export (0 if unversioned).

        """
        
        tk_flame_export_no_ui = self.import_module("tk_flame_export_no_ui")
        
        asset_type = info["assetType"]
        segment_name = info["name"]
        shot_name = info["shotName"]
        sequence_name = info["sequenceName"]        
        
        if asset_type not in ["video", "batch"]:
            # ignore anything that isn't video or batch
            return
        
        metadata = self._shots[sequence_name][shot_name]
        if segment_name not in metadata.segment_metadata:
            # create new metadata to represent this segment
            metadata.segment_metadata[segment_name] = tk_flame_export_no_ui.SegmentMetadata()

        segment_metadata = metadata.segment_metadata[segment_name]
        
        if asset_type == "video":
            # this is a published image sequence
            segment_metadata.video_info = info
            
        elif asset_type == "batch":
            # this is a published batch file
            segment_metadata.batch_info = info
        
        # indicate that the export has reached its last stage
        self._reached_post_asset_phase = True


    def do_submission_and_summary(self, session_id, info):
        """
        Push info to Shotgun and display a summary UI.
        
        :param session_id: String which identifies which export session is being referred to.
                           This parameter makes it possible to distinguish between different 
                           export sessions running if this is needed (typically only needed for
                           expert use cases).
        
        :param info: Information about the export. Contains the keys      
                     - destinationHost: Host name where the exported files will be written to.
                     - destinationPath: Export path root.
                     - presetPath: Path to the preset used for the export.
        
        """        
        
        # calculate the cut order for each sequence
        num_created_shots = 0
        for seq in self._shots:
            # get a list of metadata objects for this shot
            shot_metadata_list = self._shots[seq].values()
            # sort it by cut in
            shot_metadata_list.sort(key=lambda x: x.new_cut_in)
            # now loop over all items and set an incrementing cut order
            cut_index = 1
            for shot_metadata in shot_metadata_list:
                if shot_metadata.created_this_session:
                    num_created_shots += 1
                shot_metadata.new_cut_order = cut_index
                cut_index += 1
        
        # we push all changes to shotgun as a single batch call
        shotgun_batch_items = []
        version_path_lookup = {}
                
        # loop over all shots in our metadata data structure
        self.log_debug("Looping over all shots and segments to submit shotgun data...")
        num_cut_changes = 0
        for seq in self._shots:
            for shot_metadata in self._shots[seq].values():
                
                self.log_debug("Looking at shot %s" % shot_metadata.name)
                
                # First, create versions for all video segments.
                #
                # register all versions that we should submit for review
                # for each shot. Note that a shot may have multiple video segments
                for segment_metadata in shot_metadata.segment_metadata.values():
                    
                    # it is possible that the user has manually cancelled the process, so
                    # it's possible that a segment doesn't have a video export associated
                    # this can happen if for example a user chooses not to overwrite an 
                    # existing file on disk. 
                    if segment_metadata.video_info:
                        
                        # we have a video submission associated with this segment!
                        path = os.path.join(segment_metadata.video_info.get("destinationPath"), 
                                            segment_metadata.video_info.get("resolvedPath"))
                        
                        # compute a version-create shotgun batch dictionary
                        sg_version_batch = self._sg_submit_helper.create_version_batch(shot_metadata.context, 
                                                                                       path, 
                                                                                       self._user_comments, 
                                                                                       None, 
                                                                                       segment_metadata.video_info["aspectRatio"])                
                        # append to our main batch listing
                        self.log_debug("Registering version: %s" % pprint.pformat(sg_version_batch))
                        shotgun_batch_items.append(sg_version_batch)
                        
                        # once the batch has been executed and the versions have been created in Shotgun,
                        # we need to update our segment metadata with the shotgun version id.
                        # in order to do that, maintain a lookup dictionary:
                        path_to_frames = sg_version_batch["data"]["sg_path_to_frames"]
                        version_path_lookup[path_to_frames] = segment_metadata
                
                # Now update frame ranges to make sure shotgun matches flame.
                #
                # ensure that we actually have frame ranges for this shot
                # it seems sometimes there are shots that don't actually contain any clips.
                # I think this is anomaly in Flame, but since we have spotted it in QA,
                # it's good to do this extra check in this code.
                if shot_metadata.new_cut_in is None or shot_metadata.new_cut_out is None:
                    self.log_warning("No frame ranges calculated for Shot %s!" % shot_metadata.shotgun_id)
                
                # has the frame range changed?
                elif shot_metadata.shotgun_cut_in != shot_metadata.new_cut_in or \
                     shot_metadata.shotgun_cut_out != shot_metadata.new_cut_out or \
                     shot_metadata.shotgun_cut_order != shot_metadata.new_cut_order:
                    
                    duration = shot_metadata.new_cut_out - shot_metadata.new_cut_in + 1
                    num_cut_changes += 1
                    sg_cut_batch = {"request_type":"update", 
                                    "entity_type": "Shot",
                                    "entity_id": shot_metadata.shotgun_id,
                                    "data":{ "sg_cut_in": shot_metadata.new_cut_in,
                                             "sg_cut_out": shot_metadata.new_cut_out,
                                             "sg_cut_duration": duration, 
                                             "sg_cut_order": shot_metadata.new_cut_order }}
                    
                    self.log_debug("Registering cut change: %s" % pprint.pformat(sg_cut_batch))
                    shotgun_batch_items.append(sg_cut_batch)
                
                else:
                    self.log_debug("No frame changes detected. Shotgun and flame are already in sync.")
        
        # now push all new versions and cut changes to Shotgun in a single batch call.
        sg_data = []
        if len(shotgun_batch_items) > 0:
            self._app.engine.show_busy("Updating Shotgun...", "Registering review and cut data...")
            try:
                self.log_debug("Pushing %s Shotgun batch items..." % len(shotgun_batch_items))
                sg_data = self.shotgun.batch(shotgun_batch_items)
                self.log_debug("...done")
            finally:
                # kill progress indicator
                self._app.engine.clear_busy()
                
        # now update the shot metadata with version ids
        for sg_entity in sg_data:
            if sg_entity["type"] == "Version":
                # using our lookup table, find the metadata object
                segment_metadata = version_path_lookup[sg_entity["path_to_frames"]]
                segment_metadata.shotgun_version = sg_entity
        
        
        # now submit backburner jobs
        #
        # first, push a backburner job that will register all publishes in Shotgun given our shot metadata
        self.engine.create_local_backburner_job("Shotgun Publish", 
                                                "Generates publishes in Shotgun.", 
                                                None, 
                                                self, 
                                                "backburner_register_publishes", 
                                                args = {"metadata": self._shots})
        
        # secondly, push a quicktime generation job for each version we submitted
        for seq in self._shots:
            for shot_metadata in self._shots[seq].values():
                for segment_metadata in shot_metadata.segment_metadata.values():
                    if segment_metadata.video_info and segment_metadata.shotgun_version:
                        # this segment has video and has a version!
                        # schedule quicktime generation!
                        
                        job_title = "Shot %s - Shotgun Quicktime Upload" % shot_metadata.name
                        job_desc = "Generating quicktimes and uploading to Shotgun."         
                        
                        # if the video media is generated in a backburner job, make sure that 
                        # our quicktime job is executed *after* this job has finished                        
                        if segment_metadata.video_info.get("isBackground"):
                            run_after_job_id = segment_metadata.video_info.get("backgroundJobId")
                        else:
                            run_after_job_id = None
        
                        path = os.path.join(segment_metadata.video_info.get("destinationPath"), 
                                            segment_metadata.video_info.get("resolvedPath"))
                        
                        args = {"version_id": segment_metadata.shotgun_version["id"], 
                                "path": path,
                                "width": segment_metadata.video_info.get("width"),
                                "height": segment_metadata.video_info.get("height")
                                }
        
                        # kick off backburner job
                        self.engine.create_local_backburner_job(job_title, 
                                                                job_desc, 
                                                                run_after_job_id, 
                                                                self, 
                                                                "backburner_generate_quicktime", 
                                                                args)

        
        # now, as a very last step, show a summary UI to the user, including a 
        # very brief overview of what changes have been carried out.
        comments = "Your export has been pushed to the Backburner queue for processing.<br><br>"
        
        if num_created_shots == 1:
            comments += "- A new Shot was created in Shotgun. <br>"
        elif num_created_shots > 1:
            comments += "- %d new Shots were created in Shotgun. <br>" % num_created_shots 
            
        num_cut_updates = (num_cut_changes - num_created_shots)
        if num_cut_updates == 1:
            comments += "- One Shot had its cut information updated. <br>"
        elif num_cut_updates > 1:
            comments += "- %d Shots had their cut information updated. <br>" % num_cut_updates 
                
        tk_flame_export = self.import_module("tk_flame_export")
        self.engine.show_modal("Submission Summary", 
                               self, 
                               tk_flame_export.SummaryDialog, 
                               comments,
                               self._reached_post_asset_phase)
        
        
        


    ##############################################################################################################
    # Flare / batch mode integration

    def __is_rendering_tk_session(self, batch_path, render_path):
        """
        Determines if a batch export is outputting to file locations
        known to the current tk export app. In that case, the context
        is returned.
        
        :param batch_path: Path to the exported batch file
        :param render_path: Path to the current render (w flame sequence markers)
        :returns: Context or None if path isn't recognized 
        """
        
        # first check if the resolved paths match our templates in the settings.
        # otherwise ignore the export
        self.log_debug("Checking if the render path '%s' is recognized by toolkit..." % render_path)
        matching = False
        for preset in self.get_setting("plate_presets"):
            plate_template_name = preset["template"]
            template = self.get_template_by_name(plate_template_name)
            if template.validate(render_path):
                matching |= True
                self.log_debug(" - Matching: '%s'" % template)
            else:
                self.log_debug(" - Not matching: '%s'" % template)

        if not matching:
            self.log_debug("This path does not appear to match any toolkit render paths. Ignoring.")
            return None

        
        batch_template = self.get_template("batch_template")
        if not batch_template.validate(batch_path):
            self.log_debug("The path '%s' does not match the template '%s'. Ignoring." % (batch_path, batch_template))
            return None

        # now extract the context for the currently worked on thing
        # we do this based on the path to the batch file
        self.log_debug("Getting context from path '%s'" % batch_path)
        context = self.sgtk.context_from_path(batch_path)
        self.log_debug("Context: %s" % context)
        return context


    def pre_batch_render_checks(self, info):
        """
        Called before rendering starts in batch/flare.
        
        This pops up a UI asking the user if they want to send things to review.
        
        :param info: Dictionary with a number of parameters:
        
            nodeName:             Name of the export node.   
            exportPath:           Export path as entered in the application UI.
                                  Can be modified by the hook to change where the file are written.
            namePattern:          List of optional naming tokens as entered in the application UI.
            resolvedPath:         Full file pattern that will be exported with all the tokens resolved.
            firstFrame:           Frame number of the first frame that will be exported.
            lastFrame:            Frame number of the last frame that will be exported.
            versionName:          Current version name of export (Empty if unversioned).
            versionNumber:        Current version number of export (0 if unversioned).
            openClipNamePattern:  List of optional naming tokens pointing to the open clip created if any
                                  as entered in the application UI. This is only available if versioning
                                  is enabled.
            openClipResolvedPath: Full path to the open clip created if any with all the tokens resolved.
                                  This is only available if versioning is enabled.
            setupNamePattern:     List of optional naming tokens pointing to the setup created if any
                                  as entered in the application UI. This is only available if versioning
                                  is enabled.
            setupResolvedPath:    Full path to the setup created if any with all the tokens resolved.
                                  This is only available if versioning is enabled.
            aborted:              Indicate if the export has been aborted by the user.
            lastFrame:            Last frame rendered
            firstFrame:           First frame rendered
            fps:                  Frame rate of render
            aspectRatio:          Frame aspect ratio
            width:                Frame width
            height:               Frame height
            depth:                Frame depth ( '8-bits', '10-bits', '12-bits', '16 fp' )
            scanForamt:           Scan format ( 'FIELD_1', 'FIELD_2', 'PROGRESSIVE' )        
        """
        self._send_batch_render_to_review = False
        self._user_comments = None
        
        plate_path = os.path.join(info.get("exportPath"), info.get("resolvedPath"))
        batch_path = info.get("setupResolvedPath")
        ctx = self.__is_rendering_tk_session(batch_path, plate_path)
        if ctx is None:
            # not known by this app
            return
        
        # ok so this looks like one of our renders - check with the user 
        # if they want to submit to review!
        from PySide import QtGui, QtCore
         
        # pop up a UI asking the user for description
        tk_flame_export = self.import_module("tk_flame_export")        
        (return_code, widget) = self.engine.show_modal("Send to Review", self, tk_flame_export.BatchRenderDialog)
        
        if return_code != QtGui.QDialog.Rejected:
            # user wants review!
            self._send_batch_render_to_review = True
            self._user_comments = widget.get_comments()


    def post_batch_render_sg_process(self, info):
        """
        Called when batch rendering has finished.
        
        :param info: Dictionary with a number of parameters:
        
            nodeName:             Name of the export node.   
            exportPath:           Export path as entered in the application UI.
                                  Can be modified by the hook to change where the file are written.
            namePattern:          List of optional naming tokens as entered in the application UI.
            resolvedPath:         Full file pattern that will be exported with all the tokens resolved.
            firstFrame:           Frame number of the first frame that will be exported.
            lastFrame:            Frame number of the last frame that will be exported.
            versionName:          Current version name of export (Empty if unversioned).
            versionNumber:        Current version number of export (0 if unversioned).
            openClipNamePattern:  List of optional naming tokens pointing to the open clip created if any
                                  as entered in the application UI. This is only available if versioning
                                  is enabled.
            openClipResolvedPath: Full path to the open clip created if any with all the tokens resolved.
                                  This is only available if versioning is enabled.
            setupNamePattern:     List of optional naming tokens pointing to the setup created if any
                                  as entered in the application UI. This is only available if versioning
                                  is enabled.
            setupResolvedPath:    Full path to the setup created if any with all the tokens resolved.
                                  This is only available if versioning is enabled.
            aborted:              Indicate if the export has been aborted by the user.
            lastFrame:            Last frame rendered
            firstFrame:           First frame rendered
            fps:                  Frame rate of render
            aspectRatio:          Frame aspect ratio
            width:                Frame width
            height:               Frame height
            depth:                Frame depth ( '8-bits', '10-bits', '12-bits', '16 fp' )
            scanFormat:           Scan format ( 'FIELD_1', 'FIELD_2', 'PROGRESSIVE' )
            aborted:              Indicate if the export has been aborted by the user.
        """
        
        if "aborted" in info and info["aborted"]:
            self.log_debug("Rendering was aborted. Will not push to Shotgun.")
            return 
        
        plate_path = os.path.join(info.get("exportPath"), info.get("resolvedPath"))
        batch_path = info.get("setupResolvedPath")
        ctx = self.__is_rendering_tk_session(batch_path, plate_path)
        if ctx is None:
            # not known by this app
            return
        
        # now start preparing a remote job
        args = {"info": info, 
                "serialized_context": sgtk.context.serialize(ctx), 
                "comments": self._user_comments,
                "send_to_review": self._send_batch_render_to_review }
        
        # and populate backburner job parameters
        job_title = "%s - Shotgun Upload" % info.get("nodeName")
        job_desc = "Generating quicktime and uploading to Shotgun."
        
        # kick off async job
        self.engine.create_local_backburner_job(job_title, 
                                                job_desc, 
                                                None, # run_after_job_id 
                                                self, 
                                                "backburner_process_rendered_batch", 
                                                args)



    ##############################################################################################################
    # backburner callbacks

    def backburner_register_publishes(self, metadata):
        """
        Generate publishes in Shotgun for the given shot metadata list
        
        :param metadata: dictionary of shot metadata, on the form
                        { "Sequence_x": { "Shot_x_1":  ShotMetadata,
                                          "Shot_x_2":  ShotMetadata,
                                          "Shot_x_2":  ShotMetadata }}
        """
        self.log_debug("Creating publishes for all export items.")
        

    
    
    
    
    
    def backburner_generate_quicktime(self, version_id, path, width, height):
        """
        Backburner job. Generates a quicktime and uploads it to Shotgun.
        
        :param version_id: Shotgun version id
        :param path: Path to source media
        :param width: Width of source
        :param height: Height of source 
        """
        self._sg_submit_helper.upload_quicktime(version_id, path, width, height)        

    def backburner_process_rendered_batch(self, info, serialized_context, comments, send_to_review):
        """
        Backburner job. Takes a newly generated render and processes it for Shotgun:
        
        - registers a publish for the newly created batch file
        - registers a publish for the generated render source data
        - optionally, creates a version and uploads a quicktime.
        
        :param info: Dictionary with a number of parameters:
        
            nodeName:             Name of the export node.   
            exportPath:           Export path as entered in the application UI.
                                  Can be modified by the hook to change where the file are written.
            namePattern:          List of optional naming tokens as entered in the application UI.
            resolvedPath:         Full file pattern that will be exported with all the tokens resolved.
            firstFrame:           Frame number of the first frame that will be exported.
            lastFrame:            Frame number of the last frame that will be exported.
            versionName:          Current version name of export (Empty if unversioned).
            versionNumber:        Current version number of export (0 if unversioned).
            openClipNamePattern:  List of optional naming tokens pointing to the open clip created if any
                                  as entered in the application UI. This is only available if versioning
                                  is enabled.
            openClipResolvedPath: Full path to the open clip created if any with all the tokens resolved.
                                  This is only available if versioning is enabled.
            setupNamePattern:     List of optional naming tokens pointing to the setup created if any
                                  as entered in the application UI. This is only available if versioning
                                  is enabled.
            setupResolvedPath:    Full path to the setup created if any with all the tokens resolved.
                                  This is only available if versioning is enabled.
            aborted:              Indicate if the export has been aborted by the user.
            lastFrame:            Last frame rendered
            firstFrame:           First frame rendered
            fps:                  Frame rate of render
            aspectRatio:          Frame aspect ratio
            width:                Frame width
            height:               Frame height
            depth:                Frame depth ( '8-bits', '10-bits', '12-bits', '16 fp' )
            scanForamt:           Scan format ( 'FIELD_1', 'FIELD_2', 'PROGRESSIVE' ) 
            
        :param serialized_context: The context for the shot that the submission 
                                   is associated with, in serialized form.
        :param comments: User comments, as a string
        :param send_to_review: Boolean to indicate that we should send to sg review.            
        """
        context = sgtk.context.deserialize(serialized_context)
        version_number = int(info["versionNumber"])
        description = comments or "Automatic Flame batch render"
        
        # first register the batch file as a publish in Shotgun
        batch_path = info.get("setupResolvedPath")
        self._sg_submit_helper.register_batch_publish(context, batch_path, description, version_number)

        # Now register the rendered images as a published plate in Shotgun
        full_flame_plate_path = os.path.join(info.get("exportPath"), info.get("resolvedPath"))
        sg_data = self._sg_submit_helper.register_video_publish(context, 
                                                                full_flame_plate_path, 
                                                                description,
                                                                version_number, 
                                                                info["width"], 
                                                                info["height"], 
                                                                make_shot_thumb=False)
        
        # Finally, create a version record in Shotgun, generate a quicktime and upload it
        if send_to_review:
            
            # create sg data
            sg_version_data = self._sg_submit_helper.create_version(context, 
                                                                    full_flame_plate_path,
                                                                    description,
                                                                    sg_data, 
                                                                    info["aspectRatio"])     
            
            # and generate a quicktime
            self._sg_submit_helper.upload_quicktime(sg_version_data["id"], 
                                                    full_flame_plate_path, 
                                                    info["width"], 
                                                    info["height"])


