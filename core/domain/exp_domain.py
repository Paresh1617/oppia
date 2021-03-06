# coding: utf-8
#
# Copyright 2014 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Domain objects for an exploration, its states, and their constituents.

Domain objects capture domain-specific logic and are agnostic of how the
objects they represent are stored. All methods and properties in this file
should therefore be independent of the specific storage models used.
"""

import collections
import copy
import logging
import re
import string

from constants import constants
from core.domain import html_cleaner
from core.domain import gadget_registry
from core.domain import interaction_registry
from core.domain import param_domain
from core.domain import trigger_registry
import feconf
import jinja_utils
import schema_utils
import utils


# Do not modify the values of these constants. This is to preserve backwards
# compatibility with previous change dicts.
# TODO(bhenning): Prior to July 2015, exploration changes involving rules were
# logged using the key 'widget_handlers'. These need to be migrated to
# 'answer_groups' and 'default_outcome'.
STATE_PROPERTY_PARAM_CHANGES = 'param_changes'
STATE_PROPERTY_CONTENT = 'content'
STATE_PROPERTY_INTERACTION_ID = 'widget_id'
STATE_PROPERTY_INTERACTION_CUST_ARGS = 'widget_customization_args'
STATE_PROPERTY_INTERACTION_ANSWER_GROUPS = 'answer_groups'
STATE_PROPERTY_INTERACTION_DEFAULT_OUTCOME = 'default_outcome'
STATE_PROPERTY_UNCLASSIFIED_ANSWERS = (
    'confirmed_unclassified_answers')
STATE_PROPERTY_INTERACTION_FALLBACKS = 'fallbacks'
STATE_PROPERTY_INTERACTION_HINTS = 'hints'
STATE_PROPERTY_INTERACTION_SOLUTION = 'solution'
# These two properties are kept for legacy purposes and are not used anymore.
STATE_PROPERTY_INTERACTION_HANDLERS = 'widget_handlers'
STATE_PROPERTY_INTERACTION_STICKY = 'widget_sticky'

GADGET_PROPERTY_VISIBILITY = 'gadget_visibility'
GADGET_PROPERTY_CUST_ARGS = 'gadget_customization_args'

# This takes an additional 'state_name' parameter.
CMD_ADD_STATE = 'add_state'
# This takes additional 'old_state_name' and 'new_state_name' parameters.
CMD_RENAME_STATE = 'rename_state'
# This takes an additional 'state_name' parameter.
CMD_DELETE_STATE = 'delete_state'
# This takes additional 'property_name' and 'new_value' parameters.
CMD_EDIT_STATE_PROPERTY = 'edit_state_property'
# This takes an additional 'gadget_name' parameter.
CMD_ADD_GADGET = 'add_gadget'
# This takes additional 'old_gadget_name' and 'new_gadget_name' parameters.
CMD_RENAME_GADGET = 'rename_gadget'
# This takes an additional 'gadget_name' parameter.
CMD_DELETE_GADGET = 'delete_gadget'
# This takes additional 'property_name' and 'new_value' parameters.
CMD_EDIT_GADGET_PROPERTY = 'edit_gadget_property'
# This takes additional 'property_name' and 'new_value' parameters.
CMD_EDIT_EXPLORATION_PROPERTY = 'edit_exploration_property'
# This takes additional 'from_version' and 'to_version' parameters for logging.
CMD_MIGRATE_STATES_SCHEMA_TO_LATEST_VERSION = (
    'migrate_states_schema_to_latest_version')

# These are categories to which answers may be classified. These values should
# not be changed because they are persisted in the data store within answer
# logs.

# Represents answers classified using rules defined as part of an interaction.
EXPLICIT_CLASSIFICATION = 'explicit'
# Represents answers which are contained within the training data of an answer
# group.
TRAINING_DATA_CLASSIFICATION = 'training_data_match'
# Represents answers which were predicted using a statistical training model
# from training data within an answer group.
STATISTICAL_CLASSIFICATION = 'statistical_classifier'
# Represents answers which led to the 'default outcome' of an interaction,
# rather than belonging to a specific answer group.
DEFAULT_OUTCOME_CLASSIFICATION = 'default_outcome'

# This represents the stringified version of a rule which uses statistical
# classification for evaluation. Answers which are matched to rules with this
# rulespec will be stored with the STATISTICAL_CLASSIFICATION category.
RULE_TYPE_CLASSIFIER = 'FuzzyMatches'


def _get_full_customization_args(customization_args, ca_specs):
    """Populates the given customization_args dict with default values
    if any of the expected customization_args are missing.

    Args:
        customization_args: dict. The customization dict. The keys are names of
            customization_args and the values are dicts with a
            single key, 'value', whose corresponding value is the value of
            the customization arg.
        ca_specs: list(dict). List of spec dictionaries. Is used to check if
            some keys are missing in customization_args. Dicts have the
            following structure:
                - name: str. The customization variable name.
                - description: str. The customization variable description.
                - default_value: *. The default value of the customization
                    variable.

    Returns:
        dict. The customization_args dict where missing keys are populated with
        the default values.
    """
    for ca_spec in ca_specs:
        if ca_spec.name not in customization_args:
            customization_args[ca_spec.name] = {
                'value': ca_spec.default_value
            }
    return customization_args


def _validate_customization_args_and_values(
        item_name, item_type, customization_args,
        ca_specs_to_validate_against):
    """Validates the given `customization_args` dict against the specs set out
    in 'ca_specs_to_validate_against'. 'item_name' and 'item_type' are used to
    populate any error messages that arise during validation.
    Note that this may modify the given customization_args dict, if it has
    extra or missing keys. It also normalizes any HTML in the
    customization_args dict.

    Args:
        item_name: str. The item_name is either 'interaction', 'gadget' or
            'trigger'.
        item_type: str. The item_type is the id/type of the
            interaction/gadget/trigger, respectively.
        customization_args: dict. The customization dict. The keys are names of
            customization_args and the values are dicts with a
            single key, 'value', whose corresponding value is the value of
            the customization arg.
        ca_specs_to_validate_against: list(dict). List of spec dictionaries. Is
            used to check if some keys are missing in customization_args. Dicts
            have the following structure:
                - name: str. The customization variable name.
                - description: str. The customization variable description.
                - default_value: *. The default value of the customization
                    variable.

    Raises:
        ValidationError: The given 'customization_args' is not valid.
    """
    ca_spec_names = [
        ca_spec.name for ca_spec in ca_specs_to_validate_against]

    if not isinstance(customization_args, dict):
        raise utils.ValidationError(
            'Expected customization args to be a dict, received %s'
            % customization_args)

    # Validate and clean up the customization args.

    # Populate missing keys with the default values.
    customization_args = _get_full_customization_args(
        customization_args, ca_specs_to_validate_against)

    # Remove extra keys.
    extra_args = []
    for arg_name in customization_args.keys():
        if not isinstance(arg_name, basestring):
            raise utils.ValidationError(
                'Invalid customization arg name: %s' % arg_name)
        if arg_name not in ca_spec_names:
            extra_args.append(arg_name)
            logging.warning(
                '%s %s does not support customization arg %s.'
                % (item_name.capitalize(), item_type, arg_name))
    for extra_arg in extra_args:
        del customization_args[extra_arg]

    # Check that each value has the correct type.
    for ca_spec in ca_specs_to_validate_against:
        try:
            customization_args[ca_spec.name]['value'] = (
                schema_utils.normalize_against_schema(
                    customization_args[ca_spec.name]['value'],
                    ca_spec.schema))
        except Exception:
            # TODO(sll): Raise an actual exception here if parameters are not
            # involved. (If they are, can we get sample values for the state
            # context parameters?)
            pass


class ExplorationChange(object):
    """Domain object class for an exploration change.

    IMPORTANT: Ensure that all changes to this class (and how these cmds are
    interpreted in general) preserve backward-compatibility with the
    exploration snapshots in the datastore. Do not modify the definitions of
    cmd keys that already exist.

    NOTE TO DEVELOPERS: Please note that, for a brief period around
    Feb - Apr 2017, change dicts related to editing of answer groups
    accidentally stored the old_value using a ruleSpecs key instead of a
    rule_specs key. So, if you are making use of this data, make sure to
    verify the format of the old_value before doing any processing.
    """

    STATE_PROPERTIES = (
        STATE_PROPERTY_PARAM_CHANGES,
        STATE_PROPERTY_CONTENT,
        STATE_PROPERTY_INTERACTION_ID,
        STATE_PROPERTY_INTERACTION_CUST_ARGS,
        STATE_PROPERTY_INTERACTION_STICKY,
        STATE_PROPERTY_INTERACTION_HANDLERS,
        STATE_PROPERTY_INTERACTION_ANSWER_GROUPS,
        STATE_PROPERTY_INTERACTION_DEFAULT_OUTCOME,
        STATE_PROPERTY_INTERACTION_FALLBACKS,
        STATE_PROPERTY_INTERACTION_HINTS,
        STATE_PROPERTY_INTERACTION_SOLUTION,
        STATE_PROPERTY_UNCLASSIFIED_ANSWERS)

    GADGET_PROPERTIES = (
        GADGET_PROPERTY_VISIBILITY,
        GADGET_PROPERTY_CUST_ARGS)

    EXPLORATION_PROPERTIES = (
        'title', 'category', 'objective', 'language_code', 'tags',
        'blurb', 'author_notes', 'param_specs', 'param_changes',
        'init_state_name')

    def __init__(self, change_dict):
        """Initializes an ExplorationChange object from a dict.

        Args:
            change_dict: dict. Represents a command. It should have a 'cmd' key
                and one or more other keys. The keys depend on what the value
                for 'cmd' is. The possible values for 'cmd' are listed below,
                together with the other keys in the dict:
                    - 'add_state' (with state_name)
                    - 'rename_state' (with old_state_name and new_state_name)
                    - 'delete_state' (with state_name)
                    - 'edit_state_property' (with state_name, property_name,
                        new_value and, optionally, old_value)
                    - 'add_gadget' (with gadget_dict and panel)
                    - 'rename_gadget' (with old_gadget_name, new_gadget_name)
                    - 'delete_gadget' (with gadget_name)
                    - 'edit_gadget_property' (with gadget_name, property_name,
                        new_value, and optionally, old_value)
                    - 'edit_exploration_property' (with property_name,
                        new_value and, optionally, old_value)
                    - 'migrate_states_schema' (with from_version, to_version)
                For a state, property_name must be one of STATE_PROPERTIES.
                For an exploration, property_name must be one of
                EXPLORATION_PROPERTIES. For a gadget, property_name must be one
                of GADGET_PROPERTIES.

        Raises:
            Exception: The given change_dict is not valid.
        """
        if 'cmd' not in change_dict:
            raise Exception('Invalid change_dict: %s' % change_dict)
        self.cmd = change_dict['cmd']

        if self.cmd == CMD_ADD_STATE:
            self.state_name = change_dict['state_name']
        elif self.cmd == CMD_RENAME_STATE:
            self.old_state_name = change_dict['old_state_name']
            self.new_state_name = change_dict['new_state_name']
        elif self.cmd == CMD_DELETE_STATE:
            self.state_name = change_dict['state_name']
        elif self.cmd == CMD_EDIT_STATE_PROPERTY:
            if change_dict['property_name'] not in self.STATE_PROPERTIES:
                raise Exception('Invalid change_dict: %s' % change_dict)
            self.state_name = change_dict['state_name']
            self.property_name = change_dict['property_name']
            self.new_value = change_dict['new_value']
            self.old_value = change_dict.get('old_value')
        elif self.cmd == CMD_EDIT_EXPLORATION_PROPERTY:
            if (change_dict['property_name'] not in
                    self.EXPLORATION_PROPERTIES):
                raise Exception('Invalid change_dict: %s' % change_dict)
            self.property_name = change_dict['property_name']
            self.new_value = change_dict['new_value']
            self.old_value = change_dict.get('old_value')
        elif self.cmd == CMD_ADD_GADGET:
            self.gadget_dict = change_dict['gadget_dict']
            self.gadget_name = change_dict['gadget_dict']['gadget_name']
            self.panel = change_dict['panel']
        elif self.cmd == CMD_RENAME_GADGET:
            self.old_gadget_name = change_dict['old_gadget_name']
            self.new_gadget_name = change_dict['new_gadget_name']
        elif self.cmd == CMD_DELETE_GADGET:
            self.gadget_name = change_dict['gadget_name']
        elif self.cmd == CMD_EDIT_GADGET_PROPERTY:
            if change_dict['property_name'] not in self.GADGET_PROPERTIES:
                raise Exception('Invalid gadget change_dict: %s' % change_dict)
            self.gadget_name = change_dict['gadget_name']
            self.property_name = change_dict['property_name']
            self.new_value = change_dict['new_value']
            self.old_value = change_dict.get('old_value')
        elif self.cmd == CMD_MIGRATE_STATES_SCHEMA_TO_LATEST_VERSION:
            self.from_version = change_dict['from_version']
            self.to_version = change_dict['to_version']
        else:
            raise Exception('Invalid change_dict: %s' % change_dict)


class ExplorationCommitLogEntry(object):
    """Value object representing a commit to an exploration."""

    def __init__(
            self, created_on, last_updated, user_id, username, exploration_id,
            commit_type, commit_message, commit_cmds, version,
            post_commit_status, post_commit_community_owned,
            post_commit_is_private):
        """Initializes a ExplorationCommitLogEntry domain object.

        Args:
            created_on: datetime.datetime. Date and time when the exploration
                commit was created.
            last_updated: datetime.datetime. Date and time when the exploration
                commit was last updated.
            user_id: str. User id of the user who has made the commit.
            username: str. Username of the user who has made the commit.
            exploration_id: str. Id of the exploration.
            commit_type: str. The type of commit.
            commit_message: str. A description of changes made to the
                exploration.
            commit_cmds: list(dict). A list of commands, describing changes
                made in this model, which should give sufficient information to
                reconstruct the commit. Each dict always contains the following
                key:
                    - cmd: str. Unique command.
                and then additional arguments for that command.
            version: int. The version of the exploration after the commit.
            post_commit_status: str. The new exploration status after the
                commit.
            post_commit_community_owned: bool. Whether the exploration is
                community-owned after the edit event.
            post_commit_is_private: bool. Whether the exploration is private
                after the edit event.
        """
        self.created_on = created_on
        self.last_updated = last_updated
        self.user_id = user_id
        self.username = username
        self.exploration_id = exploration_id
        self.commit_type = commit_type
        self.commit_message = commit_message
        self.commit_cmds = commit_cmds
        self.version = version
        self.post_commit_status = post_commit_status
        self.post_commit_community_owned = post_commit_community_owned
        self.post_commit_is_private = post_commit_is_private

    def to_dict(self):
        """Returns a dict representing this ExplorationCommitLogEntry domain
        object. This omits created_on, user_id and commit_cmds.

        Returns:
            dict. A dict, mapping all fields of ExplorationCommitLogEntry
            instance, except created_on, user_id and commit_cmds fields.
        """
        return {
            'last_updated': utils.get_time_in_millisecs(self.last_updated),
            'username': self.username,
            'exploration_id': self.exploration_id,
            'commit_type': self.commit_type,
            'commit_message': self.commit_message,
            'version': self.version,
            'post_commit_status': self.post_commit_status,
            'post_commit_community_owned': self.post_commit_community_owned,
            'post_commit_is_private': self.post_commit_is_private,
        }


class AudioTranslation(object):
    """Value object representing an audio translation."""

    def to_dict(self):
        """Returns a dict representing this AudioTranslation domain object.

        Returns:
            dict. A dict, mapping all fields of AudioTranslation instance.
        """
        return {
            'filename': self.filename,
            'file_size_bytes': self.file_size_bytes,
            'needs_update': self.needs_update,
        }

    @classmethod
    def from_dict(cls, audio_translation_dict):
        """Return a AudioTranslation domain object from a dict.

        Args:
            audio_translation_dict: dict. The dict representation of
                AudioTranslation object.

        Returns:
            AudioTranslation. The corresponding AudioTranslation domain object.
        """
        return cls(
            audio_translation_dict['filename'],
            audio_translation_dict['file_size_bytes'],
            audio_translation_dict['needs_update'])

    def __init__(self, filename, file_size_bytes, needs_update):
        """Initializes a AudioTranslation domain object.

        Args:
            filename: str. The corresponding audio file path.
            file_size_bytes: int. The file size, in bytes. Used to display
                potential bandwidth usage to the learner before they download
                the file.
            needs_update: bool. Whether audio is marked for needing review.
        """
        # str. The corresponding audio file path, e.g.
        # "content-en-2-h7sjp8s.mp3".
        self.filename = filename
        # int. The file size, in bytes. Used to display potential bandwidth
        # usage to the learner before they download the file.
        self.file_size_bytes = file_size_bytes
        # bool. Whether audio is marked for needing review.
        self.needs_update = needs_update

    def validate(self):
        """Validates properties of the Content.

        Raises:
            ValidationError: One or more attributes of the AudioTranslation are
            invalid.
        """
        if not isinstance(self.filename, basestring):
            raise utils.ValidationError(
                'Expected audio filename to be a string, received %s' %
                self.filename)
        dot_index = self.filename.rfind('.')
        if dot_index == -1 or dot_index == 0:
            raise utils.ValidationError(
                'Invalid audio filename: %s' % self.filename)
        extension = self.filename[dot_index + 1:]
        if extension not in feconf.ACCEPTED_AUDIO_EXTENSIONS:
            raise utils.ValidationError(
                'Invalid audio filename: it should have one of '
                'the following extensions: %s. Received: %s',
                (feconf.ACCEPTED_AUDIO_EXTENSIONS.keys(), self.filename))

        if not isinstance(self.file_size_bytes, int):
            raise utils.ValidationError(
                'Expected file size to be an int, received %s' %
                self.file_size_bytes)
        if self.file_size_bytes <= 0:
            raise utils.ValidationError(
                'Invalid file size: %s' % self.file_size_bytes)

        if not isinstance(self.needs_update, bool):
            raise utils.ValidationError(
                'Expected needs_update to be a bool, received %s' %
                self.needs_update)


class SubtitledHtml(object):
    """Value object representing subtitled HTML."""

    def __init__(self, html, audio_translations):
        """Initializes a SubtitledHtml domain object.

        Args:
            html: str. A piece of user submitted HTML. This is cleaned in such
                a way as to contain a restricted set of HTML tags.
            audio_translations: dict(str: AudioTranslation). Dict mapping
                language codes (such as "en" or "hi") to AudioTranslation
                domain objects. Hybrid languages will be represented using
                composite language codes, such as "hi-en" for Hinglish.
        """
        self.html = html_cleaner.clean(html)
        self.audio_translations = audio_translations
        self.validate()

    def to_dict(self):
        """Returns a dict representing this SubtitledHtml domain object.

        Returns:
            dict. A dict, mapping all fields of SubtitledHtml instance.
        """
        return {
            'html': self.html,
            'audio_translations': {
                language_code: audio_translation.to_dict()
                for language_code, audio_translation
                in self.audio_translations.iteritems()
            }
        }

    @classmethod
    def from_dict(cls, subtitled_html_dict):
        """Return a SubtitledHtml domain object from a dict.

        Args:
            subtitled_html_dict: dict. The dict representation of SubtitledHtml
                object.

        Returns:
            SubtitledHtml. The corresponding SubtitledHtml domain object.
        """
        return cls(subtitled_html_dict['html'], {
            language_code: AudioTranslation.from_dict(audio_translation_dict)
            for language_code, audio_translation_dict in
            subtitled_html_dict['audio_translations'].iteritems()
        })

    def validate(self):
        """Validates properties of the SubtitledHtml.

        Raises:
            ValidationError: One or more attributes of the SubtitledHtml are
            invalid.
        """
        # TODO(sll): Add HTML sanitization checking.
        # TODO(sll): Validate customization args for rich-text components.
        if not isinstance(self.html, basestring):
            raise utils.ValidationError(
                'Invalid content HTML: %s' % self.html)

        if not isinstance(self.audio_translations, dict):
            raise utils.ValidationError(
                'Expected audio_translations to be a dict, received %s'
                % self.audio_translations)

        allowed_audio_language_codes = [
            language['id'] for language in constants.SUPPORTED_AUDIO_LANGUAGES]
        for language_code, translation in self.audio_translations.iteritems():
            if not isinstance(language_code, basestring):
                raise utils.ValidationError(
                    'Expected language code to be a string, received: %s' %
                    language_code)

            if language_code not in allowed_audio_language_codes:
                raise utils.ValidationError(
                    'Unrecognized language code: %s' % language_code)

            translation.validate()

    def to_html(self, params):
        """Exports this SubtitledHTML object to an HTML string. The HTML is
        parameterized using the parameters in `params`.

        Args:
            params: dict. The keys are the parameter names and the values are
                the values of parameters.

        Raises:
            Exception: 'params' is not a dict.
        """
        if not isinstance(params, dict):
            raise Exception(
                'Expected context params for parsing subtitled HTML to be a '
                'dict, received %s' % params)

        return html_cleaner.clean(jinja_utils.parse_string(self.html, params))


class RuleSpec(object):
    """Value object representing a rule specification."""

    def to_dict(self):
        """Returns a dict representing this RuleSpec domain object.

        Returns:
            dict. A dict, mapping all fields of RuleSpec instance.
        """
        return {
            'rule_type': self.rule_type,
            'inputs': self.inputs,
        }

    @classmethod
    def from_dict(cls, rulespec_dict):
        """Return a RuleSpec domain object from a dict.

        Args:
            rulespec_dict: dict. The dict representation of RuleSpec object.

        Returns:
            RuleSpec. The corresponding RuleSpec domain object.
        """
        return cls(
            rulespec_dict['rule_type'],
            rulespec_dict['inputs']
        )

    def __init__(self, rule_type, inputs):
        """Initializes a RuleSpec domain object.

        Args:
            rule_type: str. The rule type, e.g. "CodeContains" or "Equals". A
                full list of rule types can be found in
                extensions/interactions/rule_templates.json.
            inputs: dict. The values of the parameters needed in order to fully
                specify the rule. The keys for this dict can be deduced from
                the relevant description field in
                extensions/interactions/rule_templates.json -- they are
                enclosed in {{...}} braces.
        """
        self.rule_type = rule_type
        self.inputs = inputs

    def validate(self, rule_params_list, exp_param_specs_dict):
        """Validates a RuleSpec value object. It ensures the inputs dict does
        not refer to any non-existent parameters and that it contains values
        for all the parameters the rule expects.

        Args:
            rule_params_list: A list of parameters used by the rule represented
                by this RuleSpec instance, to be used to validate the inputs of
                this RuleSpec. Each element of the list represents a single
                parameter and is a tuple with two elements:
                    0: The name (string) of the parameter.
                    1: The typed object instance for that
                        parameter (e.g. Real).
            exp_param_specs_dict: A dict of specified parameters used in this
                exploration. Keys are parameter names and values are ParamSpec
                value objects with an object type property (obj_type). RuleSpec
                inputs may have a parameter value which refers to one of these
                exploration parameters.

        Raises:
            ValidationError: One or more attributes of the RuleSpec are
            invalid.
        """
        if not isinstance(self.inputs, dict):
            raise utils.ValidationError(
                'Expected inputs to be a dict, received %s' % self.inputs)
        input_key_set = set(self.inputs.keys())
        param_names_set = set([rp[0] for rp in rule_params_list])
        leftover_input_keys = input_key_set - param_names_set
        leftover_param_names = param_names_set - input_key_set

        # Check if there are input keys which are not rule parameters.
        if leftover_input_keys:
            logging.warning(
                'RuleSpec \'%s\' has inputs which are not recognized '
                'parameter names: %s' % (self.rule_type, leftover_input_keys))

        # Check if there are missing parameters.
        if leftover_param_names:
            raise utils.ValidationError(
                'RuleSpec \'%s\' is missing inputs: %s'
                % (self.rule_type, leftover_param_names))

        rule_params_dict = {rp[0]: rp[1] for rp in rule_params_list}
        for (param_name, param_value) in self.inputs.iteritems():
            param_obj = rule_params_dict[param_name]
            # Validate the parameter type given the value.
            if isinstance(param_value, basestring) and '{{' in param_value:
                # Value refers to a parameter spec. Cross-validate the type of
                # the parameter spec with the rule parameter.
                start_brace_index = param_value.index('{{') + 2
                end_brace_index = param_value.index('}}')
                param_spec_name = param_value[
                    start_brace_index:end_brace_index]
                if param_spec_name not in exp_param_specs_dict:
                    raise utils.ValidationError(
                        'RuleSpec \'%s\' has an input with name \'%s\' which '
                        'refers to an unknown parameter within the '
                        'exploration: %s' % (
                            self.rule_type, param_name, param_spec_name))
                # TODO(bhenning): The obj_type of the param_spec
                # (exp_param_specs_dict[param_spec_name]) should be validated
                # to be the same as param_obj.__name__ to ensure the rule spec
                # can accept the type of the parameter.
            else:
                # Otherwise, a simple parameter value needs to be normalizable
                # by the parameter object in order to be valid.
                param_obj.normalize(param_value)


class Outcome(object):
    """Value object representing an outcome of an interaction. An outcome
    consists of a destination state, feedback to show the user, and any
    parameter changes.
    """
    def to_dict(self):
        """Returns a dict representing this Outcome domain object.

        Returns:
            dict. A dict, mapping all fields of Outcome instance.
        """
        return {
            'dest': self.dest,
            'feedback': self.feedback,
            'param_changes': [param_change.to_dict()
                              for param_change in self.param_changes],
        }

    @classmethod
    def from_dict(cls, outcome_dict):
        """Return a Outcome domain object from a dict.

        Args:
            outcome_dict: dict. The dict representation of Outcome object.

        Returns:
            Outcome. The corresponding Outcome domain object.
        """
        return cls(
            outcome_dict['dest'],
            outcome_dict['feedback'],
            [param_domain.ParamChange(
                param_change['name'], param_change['generator_id'],
                param_change['customization_args'])
             for param_change in outcome_dict['param_changes']],
        )

    def __init__(self, dest, feedback, param_changes):
        """Initializes a Outcome domain object.

        Args:
            dest: str. The name of the destination state.
            feedback: list(str). List of feedback to show the user if this rule
                is triggered.
            param_changes: list(ParamChange). List of exploration-level
                parameter changes to make if this rule is triggered.
        """
        # Id of the destination state.
        # TODO(sll): Check that this state actually exists.
        self.dest = dest
        # Feedback to give the reader if this rule is triggered.
        self.feedback = feedback or []
        self.feedback = [
            html_cleaner.clean(feedback_item)
            for feedback_item in self.feedback]
        # Exploration-level parameter changes to make if this rule is
        # triggered.
        self.param_changes = param_changes or []

    def validate(self):
        """Validates various properties of the Outcome.

        Raises:
            ValidationError: One or more attributes of the Outcome are invalid.
        """
        if not self.dest:
            raise utils.ValidationError(
                'Every outcome should have a destination.')
        if not isinstance(self.dest, basestring):
            raise utils.ValidationError(
                'Expected outcome dest to be a string, received %s'
                % self.dest)

        if not isinstance(self.feedback, list):
            raise utils.ValidationError(
                'Expected outcome feedback to be a list, received %s'
                % self.feedback)
        for feedback_item in self.feedback:
            if not isinstance(feedback_item, basestring):
                raise utils.ValidationError(
                    'Expected outcome feedback item to be a string, received '
                    '%s' % feedback_item)

        if not isinstance(self.param_changes, list):
            raise utils.ValidationError(
                'Expected outcome param_changes to be a list, received %s'
                % self.param_changes)
        for param_change in self.param_changes:
            param_change.validate()


class AnswerGroup(object):
    """Value object for an answer group. Answer groups represent a set of rules
    dictating whether a shared feedback should be shared with the user. These
    rules are ORed together. Answer groups may also support a classifier
    that involve soft matching of answers to a set of training data and/or
    example answers dictated by the creator.
    """
    def to_dict(self):
        """Returns a dict representing this AnswerGroup domain object.

        Returns:
            dict. A dict, mapping all fields of AnswerGroup instance.
        """
        return {
            'rule_specs': [rule_spec.to_dict()
                           for rule_spec in self.rule_specs],
            'outcome': self.outcome.to_dict(),
            'correct': self.correct,
        }

    @classmethod
    def from_dict(cls, answer_group_dict):
        """Return a AnswerGroup domain object from a dict.

        Args:
            answer_group_dict: dict. The dict representation of AnswerGroup
                object.

        Returns:
            AnswerGroup. The corresponding AnswerGroup domain object.
        """
        return cls(
            Outcome.from_dict(answer_group_dict['outcome']),
            [RuleSpec.from_dict(rs) for rs in answer_group_dict['rule_specs']],
            answer_group_dict['correct'],
        )

    def __init__(self, outcome, rule_specs, correct):
        """Initializes a AnswerGroup domain object.

        Args:
            outcome: Outcome. The outcome corresponding to the answer group.
            rule_specs: list(RuleSpec). List of rule specifications.
            correct: bool. Whether this answer group represents a "correct"
                answer.
        """
        self.rule_specs = [RuleSpec(
            rule_spec.rule_type, rule_spec.inputs
        ) for rule_spec in rule_specs]

        self.outcome = outcome
        self.correct = correct

    def validate(self, interaction, exp_param_specs_dict):
        """Verifies that all rule classes are valid, and that the AnswerGroup
        only has one classifier rule.

        Args:
            exp_param_specs_dict: dict. A dict of all parameters used in the
                exploration. Keys are parameter names and values are ParamSpec
                value objects with an object type property (obj_type).

        Raises:
            ValidationError: One or more attributes of the AnswerGroup are
                invalid.
            ValidationError: The AnswerGroup contains more than one classifier
                rule.
        """
        if not isinstance(self.rule_specs, list):
            raise utils.ValidationError(
                'Expected answer group rules to be a list, received %s'
                % self.rule_specs)
        if len(self.rule_specs) < 1:
            raise utils.ValidationError(
                'There must be at least one rule for each answer group.')
        if not isinstance(self.correct, bool):
            raise utils.ValidationError(
                'The "correct" field should be a boolean, received %s'
                % self.correct)

        seen_classifier_rule = False
        for rule_spec in self.rule_specs:
            if rule_spec.rule_type not in interaction.rules_dict:
                raise utils.ValidationError(
                    'Unrecognized rule type: %s' % rule_spec.rule_type)

            if rule_spec.rule_type == RULE_TYPE_CLASSIFIER:
                if seen_classifier_rule:
                    raise utils.ValidationError(
                        'AnswerGroups can only have one classifier rule.')
                seen_classifier_rule = True

            rule_spec.validate(
                interaction.get_rule_param_list(rule_spec.rule_type),
                exp_param_specs_dict)

        self.outcome.validate()

    def get_classifier_rule_index(self):
        """Gets the index of the classifier in the answer groups.

        Returns:
            int or None. The index of the classifier in the answer
            groups, or None if it doesn't exist.
        """
        for (rule_spec_index, rule_spec) in enumerate(self.rule_specs):
            if rule_spec.rule_type == RULE_TYPE_CLASSIFIER:
                return rule_spec_index
        return None


class TriggerInstance(object):
    """Value object representing a trigger.

    A trigger refers to a condition that may arise during a learner
    playthrough, such as a certain number of loop-arounds on the current state,
    or a certain amount of time having elapsed.
    """
    def __init__(self, trigger_type, customization_args):
        """Initializes a TriggerInstance domain object.

        Args:
            trigger_type: str. The type of trigger.
            customization_args: dict. The customization dict. The keys are
                names of customization_args and the values are dicts with a
                single key, 'value', whose corresponding value is the value of
                the customization arg.
        """
        # A string denoting the type of trigger.
        self.trigger_type = trigger_type
        # Customization args for the trigger. This is a dict: the keys and
        # values are the names of the customization_args for this trigger
        # type, and the corresponding values for this instance of the trigger,
        # respectively. The values consist of standard Python/JSON data types
        # (i.e. strings, ints, booleans, dicts and lists, but not objects).
        self.customization_args = customization_args

    def to_dict(self):
        """Returns a dict representing this TriggerInstance domain object.

        Returns:
            dict. A dict mapping all fields of TriggerInstance instance.
        """
        return {
            'trigger_type': self.trigger_type,
            'customization_args': self.customization_args,
        }

    @classmethod
    def from_dict(cls, trigger_dict):
        """Return a TriggerInstance domain object from a dict.

        Args:
            trigger_dict: dict. The dict representation of TriggerInstance
                object.

        Returns:
            TriggerInstance. The corresponding TriggerInstance domain object.
        """
        return cls(
            trigger_dict['trigger_type'],
            trigger_dict['customization_args'])

    def validate(self):
        """Validates various properties of the TriggerInstance.

        Raises:
            ValidationError: One or more attributes of the TriggerInstance are
            invalid.
        """
        if not isinstance(self.trigger_type, basestring):
            raise utils.ValidationError(
                'Expected trigger type to be a string, received %s' %
                self.trigger_type)

        try:
            trigger = trigger_registry.Registry.get_trigger(self.trigger_type)
        except KeyError:
            raise utils.ValidationError(
                'Unknown trigger type: %s' % self.trigger_type)

        # Verify that the customization args are valid.
        _validate_customization_args_and_values(
            'trigger', self.trigger_type, self.customization_args,
            trigger.customization_arg_specs)


class Fallback(object):
    """Value object representing a fallback.

    A fallback consists of a trigger and an outcome. When the trigger is
    satisfied, the user flow is rerouted to the given outcome.
    """
    def __init__(self, trigger, outcome):
        """Initializes a Fallback domain object.

        Args:
            trigger: TriggerInstance. The satisfied trigger.
            outcome: Outcome. The outcome to apply when the user hits the
                trigger.
        """
        self.trigger = trigger
        self.outcome = outcome

    def to_dict(self):
        """Returns a dict representing this Fallback domain object.

        Returns:
            dict. A dict mapping all fields of Fallback instance.
        """
        return {
            'trigger': self.trigger.to_dict(),
            'outcome': self.outcome.to_dict(),
        }

    @classmethod
    def from_dict(cls, fallback_dict):
        """Return a Fallback domain object from a dict.

        Args:
            fallback_dict: dict. The dict representation of Fallback object.

        Returns:
            Fallback. The corresponding Fallback domain object.
        """
        return cls(
            TriggerInstance.from_dict(fallback_dict['trigger']),
            Outcome.from_dict(fallback_dict['outcome']))

    def validate(self):
        self.trigger.validate()
        self.outcome.validate()


class Hint(object):
    """Value object representing a hint."""

    def __init__(self, hint_text):
        """Constructs a Hint domain object.

        Args:
            hint_text: str. The hint text.
        """
        self.hint_text = html_cleaner.clean(hint_text)

    def to_dict(self):
        """Returns a dict representing this Hint domain object.

        Returns:
            dict. A dict mapping the field of Hint instance.
        """
        return {
            'hint_text': self.hint_text,
        }

    @classmethod
    def from_dict(cls, hint_dict):
        """Return a Hint domain object from a dict.

        Args:
            hint_dict: dict. The dict representation of Hint object.

        Returns:
            Hint. The corresponding Hint domain object.
        """
        return cls(hint_dict['hint_text'])

    def validate(self):
        """Validates all properties of Hint.

        Raises:
            ValidationError: 'hint_text' is not a string.
        """
        if not isinstance(self.hint_text, basestring):
            raise utils.ValidationError(
                'Expected hint text to be a string, received %s' %
                self.hint_text)


class Solution(object):
    """Value object representing a solution.

    A solution consists of answer_is_exclusive, correct_answer and an
    explanation.When answer_is_exclusive is True, this indicates that it is
    the only correct answer; when it is False, this indicates that it is one
    possible answer. correct_answer records an answer that enables the learner
    to progress to the next card and explanation is an HTML string containing
    an explanation for the solution.
    """
    def __init__(self, interaction_id, answer_is_exclusive,
                 correct_answer, explanation):
        """Constructs a Solution domain object.

        Args:
            interaction_id: str. The interaction id.
            answer_is_exclusive: bool. True if is the only correct answer;
                False if is one of possible answer.
            correct_answer: str. The correct answer; this answer enables the
                learner to progress to the next card.
            explanation: str. HTML string containing an explanation for the
                solution.
        """
        self.answer_is_exclusive = answer_is_exclusive
        self.correct_answer = (
            interaction_registry.Registry.get_interaction_by_id(
                interaction_id).normalize_answer(correct_answer))
        self.explanation = html_cleaner.clean(explanation)

    def to_dict(self):
        """Returns a dict representing this Solution domain object.

        Returns:
            dict. A dict mapping all fields of Solution instance.
        """
        return {
            'answer_is_exclusive': self.answer_is_exclusive,
            'correct_answer': self.correct_answer,
            'explanation': self.explanation,
        }

    @classmethod
    def from_dict(cls, interaction_id, solution_dict):
        """Return a Solution domain object from a dict.

        Args:
            interaction_id: str. The interaction id.
            solution_dict: dict. The dict representation of Solution object.

        Returns:
            Solution. The corresponding Solution domain object.
        """
        return cls(
            interaction_id,
            solution_dict['answer_is_exclusive'],
            interaction_registry.Registry.get_interaction_by_id(
                interaction_id).normalize_answer(
                    solution_dict['correct_answer']),
            solution_dict['explanation'])

    def validate(self, interaction_id):
        """Validates all properties of Solution.

        Args:
            interaction_id: str. The interaction id.

        Raises:
            ValidationError: One or more attributes of the Solution are not
            valid.
        """
        if not isinstance(self.answer_is_exclusive, bool):
            raise utils.ValidationError(
                'Expected answer_is_exclusive to be bool, received %s' %
                self.answer_is_exclusive)
        interaction_registry.Registry.get_interaction_by_id(
            interaction_id).normalize_answer(self.correct_answer)
        if not self.explanation:
            raise utils.ValidationError(
                'Explanation must not be an empty string')
        if not isinstance(self.explanation, basestring):
            raise utils.ValidationError(
                'Expected explanation to be a string, received %s' %
                self.explanation)


class InteractionInstance(object):
    """Value object for an instance of an interaction."""

    # The default interaction used for a new state.
    _DEFAULT_INTERACTION_ID = None

    def to_dict(self):
        """Returns a dict representing this InteractionInstance domain object.

        Returns:
            dict. A dict mapping all fields of InteractionInstance instance.
        """
        return {
            'id': self.id,
            'customization_args': (
                {} if self.id is None else
                _get_full_customization_args(
                    self.customization_args,
                    interaction_registry.Registry.get_interaction_by_id(
                        self.id).customization_arg_specs)),
            'answer_groups': [group.to_dict() for group in self.answer_groups],
            'default_outcome': (
                self.default_outcome.to_dict()
                if self.default_outcome is not None
                else None),
            'confirmed_unclassified_answers': (
                self.confirmed_unclassified_answers),
            'fallbacks': [fallback.to_dict() for fallback in self.fallbacks],
            'hints': [hint.to_dict() for hint in self.hints],
            'solution': self.solution,
        }

    @classmethod
    def from_dict(cls, interaction_dict):
        """Return a InteractionInstance domain object from a dict.

        Args:
            interaction_dict: dict. The dict representation of
                InteractionInstance object.

        Returns:
            InteractionInstance. The corresponding InteractionInstance domain
            object.
        """
        default_outcome_dict = (
            Outcome.from_dict(interaction_dict['default_outcome'])
            if interaction_dict['default_outcome'] is not None else None)
        return cls(
            interaction_dict['id'],
            interaction_dict['customization_args'],
            [AnswerGroup.from_dict(h)
             for h in interaction_dict['answer_groups']],
            default_outcome_dict,
            interaction_dict['confirmed_unclassified_answers'],
            [Fallback.from_dict(f) for f in interaction_dict['fallbacks']],
            [Hint.from_dict(h) for h in interaction_dict['hints']],
            interaction_dict['solution'])

    def __init__(
            self, interaction_id, customization_args, answer_groups,
            default_outcome, confirmed_unclassified_answers,
            fallbacks, hints, solution):
        """Initializes a InteractionInstance domain object.

        Args:
            interaction_id: str. The interaction id.
            customization_args: dict. The customization dict. The keys are
                names of customization_args and the values are dicts with a
                single key, 'value', whose corresponding value is the value of
                the customization arg.
            answer_groups: list(AnswerGroup). List of answer groups of the
                interaction instance.
            default_outcome: Outcome. The default outcome of the interaction
                instance.
            confirmed_unclassified_answers: list(AnswerGroup). List of answers
                which have been confirmed to be associated with the default
                outcome.
            fallbacks: list(Fallback). List of fallbacks for this interaction.
            hints: list(Hint). List of hints for this interaction.
            solution: Solution. A possible solution for the question asked in
                this interaction.
        """
        self.id = interaction_id
        # Customization args for the interaction's view. Parts of these
        # args may be Jinja templates that refer to state parameters.
        # This is a dict: the keys are names of customization_args and the
        # values are dicts with a single key, 'value', whose corresponding
        # value is the value of the customization arg.
        self.customization_args = customization_args
        self.answer_groups = answer_groups
        self.default_outcome = default_outcome
        self.confirmed_unclassified_answers = confirmed_unclassified_answers
        self.fallbacks = fallbacks
        self.hints = hints
        self.solution = solution

    @property
    def is_terminal(self):
        """Determines if this interaction type is terminal. If no ID is set for
        this interaction, it is assumed to not be terminal.

        Returns:
            bool. Whether the interaction is terminal.
        """
        return self.id and interaction_registry.Registry.get_interaction_by_id(
            self.id).is_terminal

    def get_all_non_fallback_outcomes(self):
        """Returns a list of all non-fallback outcomes of this interaction,
        i.e. every answer group and the default outcome.

        Returns:
            list(Outcome). List of non-fallback outcomes of this interaction.
        """
        outcomes = []
        for answer_group in self.answer_groups:
            outcomes.append(answer_group.outcome)
        if self.default_outcome is not None:
            outcomes.append(self.default_outcome)
        return outcomes

    def get_all_outcomes(self):
        """Returns a list of all outcomes of this interaction, taking into
        consideration every answer group, the default outcome, and every
        fallback.

        Returns:
            list(Outcome). List of all outcomes of this interaction.
        """
        outcomes = self.get_all_non_fallback_outcomes()
        for fallback in self.fallbacks:
            outcomes.append(fallback.outcome)
        return outcomes

    def validate(self, exp_param_specs_dict):
        """Validates various properties of the InteractionInstance.

        Args:
            exp_param_specs_dict: dict. A dict of specified parameters used in
                the exploration. Keys are parameter names and values are
                ParamSpec value objects with an object type property(obj_type).
                Is used to validate AnswerGroup objects.

        Raises:
            ValidationError: One or more attributes of the InteractionInstance
            are invalid.
        """
        if not isinstance(self.id, basestring):
            raise utils.ValidationError(
                'Expected interaction id to be a string, received %s' %
                self.id)
        try:
            interaction = interaction_registry.Registry.get_interaction_by_id(
                self.id)
        except KeyError:
            raise utils.ValidationError('Invalid interaction id: %s' % self.id)

        _validate_customization_args_and_values(
            'interaction', self.id, self.customization_args,
            interaction.customization_arg_specs)

        if not isinstance(self.answer_groups, list):
            raise utils.ValidationError(
                'Expected answer groups to be a list, received %s.'
                % self.answer_groups)
        if not self.is_terminal and self.default_outcome is None:
            raise utils.ValidationError(
                'Non-terminal interactions must have a default outcome.')
        if self.is_terminal and self.default_outcome is not None:
            raise utils.ValidationError(
                'Terminal interactions must not have a default outcome.')
        if self.is_terminal and self.answer_groups:
            raise utils.ValidationError(
                'Terminal interactions must not have any answer groups.')

        for answer_group in self.answer_groups:
            answer_group.validate(interaction, exp_param_specs_dict)
        if self.default_outcome is not None:
            self.default_outcome.validate()

        if not isinstance(self.fallbacks, list):
            raise utils.ValidationError(
                'Expected fallbacks to be a list, received %s'
                % self.fallbacks)
        for fallback in self.fallbacks:
            fallback.validate()

        if not isinstance(self.hints, list):
            raise utils.ValidationError(
                'Expected hints to be a list, received %s'
                % self.hints)
        for hint in self.hints:
            hint.validate()

        if self.hints:
            if self.solution:
                Solution.from_dict(
                    self.id, self.solution).validate(self.id)

        elif self.solution:
            raise utils.ValidationError(
                'Hint(s) must be specified if solution is specified')

    @classmethod
    def create_default_interaction(cls, default_dest_state_name):
        """Create a default InteractionInstance domain object:
            - customization_args: empty dictionary;
            - answer_groups: empty list;
            - default_outcome: dest is set to 'default_dest_state_name' and
                feedback and param_changes are initialized as empty lists;
            - confirmed_unclassified_answers: empty list;
            - fallbacks: empty list;

        Args:
            default_dest_state_name: str. The default destination state.

        Returns:
            InteractionInstance. The corresponding InteractionInstance domain
            object with default values.
        """
        return cls(
            cls._DEFAULT_INTERACTION_ID,
            {}, [],
            Outcome(default_dest_state_name, [], {}), [], [], [], {}
        )


class GadgetInstance(object):
    """Value object for an instance of a gadget."""

    _MAX_GADGET_NAME_LENGTH = 20

    def __init__(self, gadget_type, gadget_name,
                 visible_in_states, customization_args):
        """Initializes a GadgetInstance domain object.

        Args:
            gadget_type: str. Backend ID referring to the gadget's type in
                gadget registry.
            gadget_name: str. The gadget name.
            visible_in_states: list(str). List of state name where this
                gadget is visible.
            customization_args: dict. The customization args for the gadget's
                view.
        """
        # Backend ID referring to the gadget's type in gadget registry.
        self.type = gadget_type

        # Author-facing unique name to distinguish instances in the Editor UI.
        # Gadgets may use this name as a title in learner facing UI as well.
        self.name = gadget_name

        # List of State name strings where this Gadget is visible.
        self.visible_in_states = visible_in_states

        # Customization args for the gadget's view.
        self.customization_args = customization_args

    @property
    def gadget(self):
        """Gets a gadget spec based on its type.

        Returns:
            GadgetInstance. The corresponding GadgetInstance domain object.
        """
        return gadget_registry.Registry.get_gadget_by_type(self.type)

    @property
    def width(self):
        """Gets the gadget width in pixels.

        Returns:
            int. The gadget width, in pixels.
        """
        return self.gadget.width_px

    @property
    def height(self):
        """Gets the gadget height in pixels.

        Returns:
            int. The gadget height, in pixels.
        """
        return self.gadget.height_px

    @staticmethod
    def _validate_gadget_name(gadget_name):
        """Validates gadget_name property of the GadgetInstance. gadget_name is
        a non-empty string of alphanumerics allowing spaces.

        Raises:
            ValidationError: gadget_name is a empty string or not alphanumeric
            or is too long.
        """
        if gadget_name == '':
            raise utils.ValidationError(
                'Gadget name must not be an empty string.')

        if not isinstance(gadget_name, basestring):
            raise utils.ValidationError(
                'Gadget name must be a string. Received type: %s' % str(
                    type(gadget_name).__name__)
            )

        if len(gadget_name) > GadgetInstance._MAX_GADGET_NAME_LENGTH:
            raise utils.ValidationError(
                '%s gadget name exceeds maximum length of %d' % (
                    gadget_name,
                    GadgetInstance._MAX_GADGET_NAME_LENGTH
                )
            )

        if not re.search(feconf.ALPHANUMERIC_SPACE_REGEX, gadget_name):
            raise utils.ValidationError(
                'Gadget names must be alphanumeric. Spaces are allowed.'
                ' Received: %s' % gadget_name
            )

    def validate(self):
        """Validates properties of the GadgetInstance.

        Raises:
            ValidationError: One or more attributes of the GadgetInstance are
            invalid.
        """
        try:
            self.gadget
        except KeyError:
            raise utils.ValidationError(
                'Unknown gadget with type %s is not in the registry.' % (
                    self.type)
            )

        self._validate_gadget_name(self.name)

        _validate_customization_args_and_values(
            'gadget', self.type, self.customization_args,
            self.gadget.customization_arg_specs)

        # Do additional per-gadget validation on the customization args.
        self.gadget.validate(self.customization_args)

        if self.visible_in_states == []:
            raise utils.ValidationError(
                '%s gadget not visible in any states.' % (
                    self.name))

        # Validate state name visibility isn't repeated within each gadget.
        if len(self.visible_in_states) != len(set(self.visible_in_states)):
            redundant_visible_states = [
                state_name for state_name, count
                in collections.Counter(self.visible_in_states).items()
                if count > 1]
            raise utils.ValidationError(
                '%s specifies visibility repeatedly for state%s: %s' % (
                    self.type,
                    's' if len(redundant_visible_states) > 1 else '',
                    ', '.join(redundant_visible_states)))

    def to_dict(self):
        """Returns a dict representing this GadgetInstance domain object.

        Returns:
            dict. A dict mapping all fields of GadgetInstance instance.
        """
        return {
            'gadget_type': self.type,
            'gadget_name': self.name,
            'visible_in_states': self.visible_in_states,
            'customization_args': _get_full_customization_args(
                self.customization_args,
                self.gadget.customization_arg_specs),
        }

    @classmethod
    def from_dict(cls, gadget_dict):
        """Return a GadgetInstance domain object from a dict.

        Args:
            gadget_dict: dict. The dict representation of GadgetInstance
                object.

        Returns:
            GadgetInstance. The corresponding GadgetInstance domain object.
        """
        return GadgetInstance(
            gadget_dict['gadget_type'],
            gadget_dict['gadget_name'],
            gadget_dict['visible_in_states'],
            gadget_dict['customization_args'])

    def update_customization_args(self, customization_args):
        """Updates the GadgetInstance's customization arguments.

        Args:
            customization_args: dict. The customization args for the gadget's
                view.
        """
        self.customization_args = customization_args

    def update_visible_in_states(self, visible_in_states):
        """Updates the GadgetInstance's visibility in different states.

        Args:
            visible_in_states: list(str). List of state names where this gadget
                is visible.
        """
        self.visible_in_states = visible_in_states

    def _get_full_customization_args(self):
        """Populates the customization_args dict of the gadget with
        default values, if any of the expected customization_args are missing.

        Returns:
            dict. The customization_args dict of the gadget.
        """
        full_customization_args_dict = copy.deepcopy(self.customization_args)

        for ca_spec in self.gadget.customization_arg_specs:
            if ca_spec.name not in full_customization_args_dict:
                full_customization_args_dict[ca_spec.name] = {
                    'value': ca_spec.default_value
                }
        return full_customization_args_dict


class SkinInstance(object):
    """Domain object for a skin instance."""

    def __init__(self, skin_id, skin_customizations):
        """Initializes SkinInstance with any customizations provided.
        If no customizations are necessary, skin_customizations may be set to
        None, in which case defaults will be generated that provide empty
        gadget panels for each panel specified in the skin.

        Args:
            skin_id: str. The id of the skin.
            skin_customizations: dict. The customization dictionary.
        """
        # TODO(sll): Deprecate this property; it is not used.
        self.skin_id = skin_id
        # panel_contents_dict has panel strings as keys and
        # lists of GadgetInstance instances as values.
        self.panel_contents_dict = {}

        default_skin_customizations = (
            SkinInstance._get_default_skin_customizations())

        # Ensure that skin_customizations is a dict.
        if skin_customizations is None:
            skin_customizations = (
                SkinInstance._get_default_skin_customizations())

        # Populate panel_contents_dict with default skin customizations
        # if they are not specified in skin_customizations.
        for panel in default_skin_customizations['panels_contents']:
            if panel not in skin_customizations['panels_contents']:
                self.panel_contents_dict[panel] = []
            else:
                self.panel_contents_dict[panel] = [
                    GadgetInstance(
                        gdict['gadget_type'],
                        gdict['gadget_name'],
                        gdict['visible_in_states'],
                        gdict['customization_args']
                    ) for gdict in skin_customizations['panels_contents'][panel]
                ]

    @staticmethod
    def _get_default_skin_customizations():
        """Generates default skin customizations when none are specified.

        Returns:
            dict. The default skin customizations.
        """
        return {
            'panels_contents': {
                panel_name: []
                for panel_name in feconf.PANELS_PROPERTIES
            }
        }

    def validate_gadget_panel(self, panel_name, gadget_list):
        """Validate proper fit given space requirements specified by
        feconf.PANELS_PROPERTIES.

        Args:
            panel_name: str. Unique name that identifies this panel in the
                skin. This should correspond to an entry in
                feconf.PANELS_PROPERTIES.
            gadget_list: list(GadgetInstance). List of GadgetInstance
                instances.

        Raises:
            ValidationError: The space requirements are not satisfied.
        """
        # If the panel contains no gadgets, max() will raise an error,
        # so we return early.
        if not gadget_list:
            return

        panel_spec = feconf.PANELS_PROPERTIES[panel_name]

        # This is a dict whose keys are state names, and whose corresponding
        # values are lists of GadgetInstance instances representing the gadgets
        # visible in that state. Note that the keys only include states for
        # which at least one gadget is visible.
        gadget_visibility_map = collections.defaultdict(list)
        for gadget_instance in gadget_list:
            for state_name in set(gadget_instance.visible_in_states):
                gadget_visibility_map[state_name].append(gadget_instance)

        # Validate limitations and fit considering visibility for each state.
        for state_name, gadget_instances in gadget_visibility_map.iteritems():
            if len(gadget_instances) > panel_spec['max_gadgets']:
                raise utils.ValidationError(
                    "'%s' panel expected at most %d gadget%s, but %d gadgets"
                    " are visible in state '%s'." % (
                        panel_name,
                        panel_spec['max_gadgets'],
                        's' if panel_spec['max_gadgets'] != 1 else '',
                        len(gadget_instances),
                        state_name))

            # Calculate total width and height of gadgets given custom args and
            # panel stackable axis.
            total_width = 0
            total_height = 0

            if (panel_spec['stackable_axis'] ==
                    feconf.GADGET_PANEL_AXIS_HORIZONTAL):
                total_width += panel_spec['pixels_between_gadgets'] * (
                    len(gadget_instances) - 1)
                total_width += sum(
                    gadget.width for gadget in gadget_instances)
                total_height = max(
                    gadget.height for gadget in gadget_instances)
            else:
                raise utils.ValidationError(
                    "Unrecognized axis for '%s' panel. ")

            # Validate fit for each dimension.
            if panel_spec['height'] < total_height:
                raise utils.ValidationError(
                    "Height %d of panel \'%s\' exceeds limit of %d" % (
                        total_height, panel_name, panel_spec['height']))
            elif panel_spec['width'] < total_width:
                raise utils.ValidationError(
                    "Width %d of panel \'%s\' exceeds limit of %d" % (
                        total_width, panel_name, panel_spec['width']))

    def validate(self):
        """Validates that gadgets fit the skin panel dimensions, and that the
        gadgets themselves are valid.

        Raises:
            ValidationError: One or more attributes of the SkinInstance are
            invalid.
        """
        # A list to validate each gadget_instance.name is unique.
        gadget_instance_names = []

        for panel_name, gadget_instances in (
                self.panel_contents_dict.iteritems()):

            # Validate existence of panels in the skin.
            if panel_name not in feconf.PANELS_PROPERTIES:
                raise utils.ValidationError(
                    'The panel name \'%s\' is invalid.' % panel_name)

            # Validate gadgets fit each skin panel.
            self.validate_gadget_panel(panel_name, gadget_instances)

            # Validate gadget internal attributes.
            for gadget_instance in gadget_instances:
                gadget_instance.validate()
                if gadget_instance.name in gadget_instance_names:
                    raise utils.ValidationError(
                        '%s gadget instance name must be unique.' % (
                            gadget_instance.name)
                    )
                gadget_instance_names.append(gadget_instance.name)

    def to_dict(self):
        """Returns a dict representing this SkinInstance domain object.

        Returns:
            dict. A dict mapping all fields of SkinInstance instance.
        """
        return {
            'skin_id': self.skin_id,
            'skin_customizations': {
                'panels_contents': {
                    panel: [
                        gadget_instance.to_dict() for gadget_instance
                        in instances_list]
                    for panel, instances_list in
                    self.panel_contents_dict.iteritems()
                },
            }
        }

    @classmethod
    def from_dict(cls, skin_dict):
        """Return a SkinInstance domain object from a dict.

        Args:
            content_dict: dict. The dict representation of SkinInstance object.

        Returns:
            SkinInstance. The corresponding SkinInstance domain object.
        """
        return SkinInstance(
            skin_dict['skin_id'],
            skin_dict['skin_customizations'])

    def get_state_names_required_by_gadgets(self):
        """Returns a list of strings representing State names required by
        GadgetInstances in this skin.

        Returns:
            list(str). List of State names required.
        """
        state_names = set()
        for gadget_instances in self.panel_contents_dict.values():
            for gadget_instance in gadget_instances:
                for state_name in gadget_instance.visible_in_states:
                    state_names.add(state_name)

        # We convert to a sorted list for clean deterministic testing.
        return sorted(state_names)


class State(object):
    """Domain object for a state."""

    NULL_INTERACTION_DICT = {
        'id': None,
        'customization_args': {},
        'answer_groups': [],
        'default_outcome': {
            'dest': feconf.DEFAULT_INIT_STATE_NAME,
            'feedback': [],
            'param_changes': [],
        },
        'confirmed_unclassified_answers': [],
        'fallbacks': [],
        'hints': [],
        'solution': {},
    }

    def __init__(self, content, param_changes, interaction,
                 classifier_model_id=None):
        """Initializes a State domain object.

        Args:
            content: list(Content). The contents displayed to the reader in
                this state. This list must have only one element.
            param_changes: list(ParamChange). Parameter changes associated with
                this state.
            interaction: InteractionInstance. The interaction instance
                associated with this state.
            classifier_model_id: str or None. The classifier model ID
                associated with this state, if applicable.
        """
        # The content displayed to the reader in this state.
        self.content = content
        # Parameter changes associated with this state.
        self.param_changes = [param_domain.ParamChange(
            param_change.name, param_change.generator.id,
            param_change.customization_args
        ) for param_change in param_changes]
        # The interaction instance associated with this state.
        self.interaction = InteractionInstance(
            interaction.id, interaction.customization_args,
            interaction.answer_groups, interaction.default_outcome,
            interaction.confirmed_unclassified_answers, interaction.fallbacks,
            interaction.hints, interaction.solution)
        self.classifier_model_id = classifier_model_id

    def validate(self, exp_param_specs_dict, allow_null_interaction):
        """Validates various properties of the State.

        Args:
            exp_param_specs_dict: dict. A dict of specified parameters used in
                this exploration. Keys are parameter names and values are
                ParamSpec value objects with an object type property(obj_type).
            allow_null_interaction. bool. Whether this state's interaction is
                allowed to be unspecified.

        Raises:
            ValidationError: One or more attributes of the State are invalid.
        """
        self.content.validate()

        if not isinstance(self.param_changes, list):
            raise utils.ValidationError(
                'Expected state param_changes to be a list, received %s'
                % self.param_changes)
        for param_change in self.param_changes:
            param_change.validate()

        if not allow_null_interaction and self.interaction.id is None:
            raise utils.ValidationError(
                'This state does not have any interaction specified.')
        elif self.interaction.id is not None:
            self.interaction.validate(exp_param_specs_dict)

    def get_training_data(self):
        """Retrieves training data from the State domain object."""
        training_data = []
        for (answer_group_index, answer_group) in enumerate(
                self.interaction.answer_groups):
            classifier_rule_spec_index = (
                answer_group.get_classifier_rule_index())
            if classifier_rule_spec_index is not None:
                classifier_rule_spec = answer_group.rule_specs[
                    classifier_rule_spec_index]
                answers = copy.deepcopy(classifier_rule_spec.inputs[
                    'training_data'])
                training_data.append({
                    'answer_group_index': answer_group_index,
                    'answers': answers
                })
        return training_data

    def can_undergo_classification(self):
        """Checks whether the answers for this state satisfy the preconditions
        for a ML model to be trained.

        Returns:
            bool: True, if the conditions are satisfied.
        """
        training_examples_count = 0
        labels_count = 0
        training_examples_count += len(
            self.interaction.confirmed_unclassified_answers)
        for answer_group in self.interaction.answer_groups:
            classifier_rule_spec_index = (
                answer_group.get_classifier_rule_index())
            if classifier_rule_spec_index is not None:
                classifier_rule_spec = answer_group.rule_specs[
                    classifier_rule_spec_index]
                training_examples_count += len(
                    classifier_rule_spec.inputs['training_data'])
                labels_count += 1
        if ((training_examples_count >= feconf.MIN_TOTAL_TRAINING_EXAMPLES) and
                (labels_count >= feconf.MIN_ASSIGNED_LABELS)):
            return True
        return False

    def update_content(self, content_dict):
        """Update the list of Content of this state.

        Args:
            content_dict. dict. The dict representation of SubtitledHtml
                object.
        """
        # TODO(sll): Must sanitize all content in RTE component attrs.
        self.content = SubtitledHtml.from_dict(content_dict)

    def update_param_changes(self, param_change_dicts):
        """Update the param_changes dict attribute.

        Args:
            param_change_dicts. list(dict). List of param_change dicts that
                represent ParamChange domain object.
        """
        self.param_changes = [
            param_domain.ParamChange.from_dict(param_change_dict)
            for param_change_dict in param_change_dicts]

    def update_interaction_id(self, interaction_id):
        """Update the interaction id attribute.

        Args:
            interaction_id. str. The new interaction id to set.
        """
        self.interaction.id = interaction_id
        # TODO(sll): This should also clear interaction.answer_groups (except
        # for the default rule). This is somewhat mitigated because the client
        # updates interaction_answer_groups directly after this, but we should
        # fix it.

    def update_interaction_customization_args(self, customization_args):
        """Update the customization_args of InteractionInstance domain object.

        Args:
            customization_args. dict. The new customization_args to set.
        """
        self.interaction.customization_args = customization_args

    def update_interaction_answer_groups(self, answer_groups_list):
        """Update the list of AnswerGroup in IteractioInstancen domain object.

        Args:
            answer_groups_list. list(dict). List of dicts that represent
                AnswerGroup domain object.
        """
        if not isinstance(answer_groups_list, list):
            raise Exception(
                'Expected interaction_answer_groups to be a list, received %s'
                % answer_groups_list)

        interaction_answer_groups = []

        # TODO(yanamal): Do additional calculations here to get the
        # parameter changes, if necessary.
        for answer_group_dict in answer_groups_list:
            rule_specs_list = answer_group_dict['rule_specs']
            if not isinstance(rule_specs_list, list):
                raise Exception(
                    'Expected answer group rule specs to be a list, '
                    'received %s' % rule_specs_list)

            answer_group = AnswerGroup(Outcome.from_dict(
                answer_group_dict['outcome']), [], answer_group_dict['correct'])
            answer_group.outcome.feedback = [
                html_cleaner.clean(feedback)
                for feedback in answer_group.outcome.feedback]
            for rule_dict in rule_specs_list:
                rule_spec = RuleSpec.from_dict(rule_dict)

                # Normalize and store the rule params.
                rule_inputs = rule_spec.inputs
                if not isinstance(rule_inputs, dict):
                    raise Exception(
                        'Expected rule_inputs to be a dict, received %s'
                        % rule_inputs)
                for param_name, value in rule_inputs.iteritems():
                    param_type = (
                        interaction_registry.Registry.get_interaction_by_id(
                            self.interaction.id
                        ).get_rule_param_type(rule_spec.rule_type, param_name))

                    if (isinstance(value, basestring) and
                            '{{' in value and '}}' in value):
                        # TODO(jacobdavis11): Create checks that all parameters
                        # referred to exist and have the correct types
                        normalized_param = value
                    else:
                        try:
                            normalized_param = param_type.normalize(value)
                        except TypeError:
                            raise Exception(
                                '%s has the wrong type. It should be a %s.' %
                                (value, param_type.__name__))
                    rule_inputs[param_name] = normalized_param

                answer_group.rule_specs.append(rule_spec)
            interaction_answer_groups.append(answer_group)
        self.interaction.answer_groups = interaction_answer_groups

    def update_interaction_default_outcome(self, default_outcome_dict):
        """Update the default_outcome of InteractionInstance domain object.

        Args:
            default_outcome_dict. dict. Dict that represents Outcome domain
                object.
        """
        if default_outcome_dict:
            if not isinstance(default_outcome_dict, dict):
                raise Exception(
                    'Expected default_outcome_dict to be a dict, received %s'
                    % default_outcome_dict)
            self.interaction.default_outcome = Outcome.from_dict(
                default_outcome_dict)
            self.interaction.default_outcome.feedback = [
                html_cleaner.clean(feedback)
                for feedback in self.interaction.default_outcome.feedback]
        else:
            self.interaction.default_outcome = None

    def update_interaction_confirmed_unclassified_answers(
            self, confirmed_unclassified_answers):
        """Update the confirmed_unclassified_answers of IteractionInstance
        domain object.

        Args:
            confirmed_unclassified_answers. list(AnswerGroup). The new list of
                answers which have been confirmed to be associated with the
                default outcome.

        Raises:
            Exception: 'confirmed_unclassified_answers' is not a list.
        """
        if not isinstance(confirmed_unclassified_answers, list):
            raise Exception(
                'Expected confirmed_unclassified_answers to be a list,'
                ' received %s' % confirmed_unclassified_answers)
        self.interaction.confirmed_unclassified_answers = (
            confirmed_unclassified_answers)

    def update_interaction_fallbacks(self, fallbacks_list):
        """Update the fallbacks of InteractionInstance domain object.

        Args:
            fallbacks_list. list(dict). List of dicts that represent Fallback
                domain object.
        """
        if not isinstance(fallbacks_list, list):
            raise Exception(
                'Expected fallbacks_list to be a list, received %s'
                % fallbacks_list)
        self.interaction.fallbacks = [
            Fallback.from_dict(fallback_dict)
            for fallback_dict in fallbacks_list]
        if self.interaction.fallbacks:
            hint_list = []
            for fallback in self.interaction.fallbacks:
                if fallback.outcome.feedback:
                    # If a fallback outcome has a non-empty feedback list
                    # the feedback is converted to a Hint. It may contain
                    # only one list item.
                    hint_list.append(
                        Hint(fallback.outcome.feedback[0]).to_dict())
        self.update_interaction_hints(hint_list)

    def update_interaction_hints(self, hints_list):
        """Update the list of hints.

        Args:
            hint_list: list(dict). A list of dict; each dict represents a Hint
                object.

        Raises:
            Exception: 'hint_list' is not a list.
        """
        if not isinstance(hints_list, list):
            raise Exception(
                'Expected hints_list to be a list, received %s'
                % hints_list)
        self.interaction.hints = [
            Hint.from_dict(hint_dict)
            for hint_dict in hints_list]

    def update_interaction_solution(self, solution_dict):
        """Update the solution of interaction.

        Args:
            solution_dict: dict. The dict representation of Solution object.

        Raises:
            Exception: 'hint_list' is not a list.
        """
        if not isinstance(solution_dict, dict):
            raise Exception(
                'Expected solution to be a dict, received %s'
                % solution_dict)
        self.interaction.solution = Solution.from_dict(
            self.interaction.id, solution_dict)

    def add_hint(self, hint_text):
        """Add a new hint to the list of hints.

        Args:
            hint_text: str. The hint text.
        """
        self.interaction.hints.append(Hint(hint_text))

    def delete_hint(self, index):
        """Delete a hint from the list of hints.

        Args:
            index: int. The position of the hint in the list of hints.

        Raises:
            IndexError: Index is less than 0.
            IndexError: Index is greater than or equal than the length of hints
                list.
        """
        if index < 0 or index >= len(self.interaction.hints):
            raise IndexError('Hint index out of range')
        del self.interaction.hints[index]

    def to_dict(self):
        """Returns a dict representing this State domain object.

        Returns:
            dict. A dict mapping all fields of State instance.
        """
        return {
            'content': self.content.to_dict(),
            'param_changes': [param_change.to_dict()
                              for param_change in self.param_changes],
            'interaction': self.interaction.to_dict(),
            'classifier_model_id': self.classifier_model_id,
        }

    @classmethod
    def from_dict(cls, state_dict):
        """Return a State domain object from a dict.

        Args:
            state_dict: dict. The dict representation of State object.

        Returns:
            State. The corresponding State domain object.
        """
        return cls(
            SubtitledHtml.from_dict(state_dict['content']),
            [param_domain.ParamChange.from_dict(param)
             for param in state_dict['param_changes']],
            InteractionInstance.from_dict(state_dict['interaction']),
            state_dict['classifier_model_id'],
        )

    @classmethod
    def create_default_state(
            cls, default_dest_state_name, is_initial_state=False):
        """Return a State domain object with default value.

        Args:
            default_dest_state_name: str. The default destination state.
            is_initial_state: bool. Whether this state represents the initial
                state of an exploration.

        Returns:
            State. The corresponding State domain object.
        """
        content_html = (
            feconf.DEFAULT_INIT_STATE_CONTENT_STR if is_initial_state else '')
        return cls(
            SubtitledHtml(content_html, {}),
            [],
            InteractionInstance.create_default_interaction(
                default_dest_state_name))


class Exploration(object):
    """Domain object for an Oppia exploration."""

    def __init__(self, exploration_id, title, category, objective,
                 language_code, tags, blurb, author_notes, skin_customizations,
                 states_schema_version, init_state_name, states_dict,
                 param_specs_dict, param_changes_list, version,
                 created_on=None, last_updated=None):
        """Initializes an Exploration domain object.

        Args:
            exploration_id: str. The exploration id.
            title: str. The exploration title.
            category: str. The category of the exploration.
            objective: str. The objective of the exploration.
            language_code: str. The language code of the exploration.
            tags: list(str). The tags given to the exploration.
            blurb: str. The blurb of the exploration.
            author_notes: str. The author notes.
            skin_customizations: dict. The customization dictionary of
                SkinInstance domain object.
            states_schema_version: int. Tbe schema version of the exploration.
            init_state_name: str. The name for the initial state of the
                exploration.
            states_dict: dict. A dict where each key-value pair represents,
                respectively, a state name and a dict used to initialize a
                State domain object.
            param_specs_dict: dict. A dict where each key-value pair represents
                respectively, a param spec name and a dict used to initialize a
                ParamSpec domain object.
            param_changes_list: list(dict). List of dict where each dict is
                used to initialize a ParamChange domain object.
            version: int. The version of the exploration.
            created_on: datetime.datetime. Date and time when the exploration
                is created.
            last_updated: datetime.datetime. Date and time when the exploration
                was last updated.
        """
        self.id = exploration_id
        self.title = title
        self.category = category
        self.objective = objective
        self.language_code = language_code
        self.tags = tags
        self.blurb = blurb
        self.author_notes = author_notes
        self.states_schema_version = states_schema_version
        self.init_state_name = init_state_name

        self.skin_instance = SkinInstance(
            feconf.DEFAULT_SKIN_ID, skin_customizations)

        self.states = {}
        for (state_name, state_dict) in states_dict.iteritems():
            self.states[state_name] = State.from_dict(state_dict)

        self.param_specs = {
            ps_name: param_domain.ParamSpec.from_dict(ps_val)
            for (ps_name, ps_val) in param_specs_dict.iteritems()
        }
        self.param_changes = [
            param_domain.ParamChange.from_dict(param_change_dict)
            for param_change_dict in param_changes_list]

        self.version = version
        self.created_on = created_on
        self.last_updated = last_updated

    @classmethod
    def create_default_exploration(
            cls, exploration_id, title=feconf.DEFAULT_EXPLORATION_TITLE,
            category=feconf.DEFAULT_EXPLORATION_CATEGORY,
            objective=feconf.DEFAULT_EXPLORATION_OBJECTIVE,
            language_code=constants.DEFAULT_LANGUAGE_CODE):
        """Returns a Exploration domain object with default values.
            'title', 'category', 'objective' if not provided are taken from
            feconf; 'tags' and 'param_changes_list' are initialized to empty
            list; 'states_schema_version' and 'init_state_name' are taken from
            feconf; 'states_dict' is derived from feconf; 'param_specs_dict' is
            an empty dict; 'blurb' and 'author_notes' are initialized to empty
            empty string; 'skin_customizations' is a null object; 'version' is
            initializated to 0.

        Args:
            exploration_id: str. The id of the exploration.
            title: str. The exploration title.
            category: str. The category of the exploration.
            objective: str. The objective of the exploration.
            language_code: str. The language code of the exploration.

        Returns:
            Exploration. The Exploration domain object with default
            values.
        """
        init_state_dict = State.create_default_state(
            feconf.DEFAULT_INIT_STATE_NAME, is_initial_state=True).to_dict()

        states_dict = {
            feconf.DEFAULT_INIT_STATE_NAME: init_state_dict
        }

        return cls(
            exploration_id, title, category, objective, language_code, [], '',
            '', None, feconf.CURRENT_EXPLORATION_STATES_SCHEMA_VERSION,
            feconf.DEFAULT_INIT_STATE_NAME, states_dict, {}, [], 0)

    @classmethod
    def from_dict(
            cls, exploration_dict,
            exploration_version=0, exploration_created_on=None,
            exploration_last_updated=None):
        """Return a Exploration domain object from a dict.

        Args:
            exploration_dict: dict. The dict representation of Exploration
                object.
            exploration_version: int. The version of the exploration.
            exploration_created_on: datetime.datetime. Date and time when the
                exploration is created.
            exploration_last_updated: datetime.datetime. Date and time when the
                exploration was last updated.

        Returns:
            Exploration. The corresponding Exploration domain object.
        """
        # NOTE TO DEVELOPERS: It is absolutely ESSENTIAL this conversion to and
        # from an ExplorationModel/dictionary MUST be exhaustive and complete.
        exploration = cls.create_default_exploration(
            exploration_dict['id'],
            title=exploration_dict['title'],
            category=exploration_dict['category'],
            objective=exploration_dict['objective'],
            language_code=exploration_dict['language_code'])
        exploration.tags = exploration_dict['tags']
        exploration.blurb = exploration_dict['blurb']
        exploration.author_notes = exploration_dict['author_notes']

        exploration.param_specs = {
            ps_name: param_domain.ParamSpec.from_dict(ps_val) for
            (ps_name, ps_val) in exploration_dict['param_specs'].iteritems()
        }

        exploration.states_schema_version = exploration_dict[
            'states_schema_version']
        init_state_name = exploration_dict['init_state_name']
        exploration.rename_state(exploration.init_state_name, init_state_name)
        exploration.add_states([
            state_name for state_name in exploration_dict['states']
            if state_name != init_state_name])

        for (state_name, sdict) in exploration_dict['states'].iteritems():
            state = exploration.states[state_name]

            state.content = SubtitledHtml(
                html_cleaner.clean(sdict['content']['html']), {
                    language_code: AudioTranslation.from_dict(translation)
                    for language_code, translation in
                    sdict['content']['audio_translations'].iteritems()
                })

            state.param_changes = [param_domain.ParamChange(
                pc['name'], pc['generator_id'], pc['customization_args']
            ) for pc in sdict['param_changes']]

            for pc in state.param_changes:
                if pc.name not in exploration.param_specs:
                    raise Exception('Parameter %s was used in a state but not '
                                    'declared in the exploration param_specs.'
                                    % pc.name)

            idict = sdict['interaction']
            interaction_answer_groups = [
                AnswerGroup.from_dict({
                    'outcome': {
                        'dest': group['outcome']['dest'],
                        'feedback': [
                            html_cleaner.clean(feedback)
                            for feedback in group['outcome']['feedback']],
                        'param_changes': group['outcome']['param_changes'],
                    },
                    'rule_specs': [{
                        'inputs': rule_spec['inputs'],
                        'rule_type': rule_spec['rule_type'],
                    } for rule_spec in group['rule_specs']],
                    'correct': False,
                })
                for group in idict['answer_groups']]

            default_outcome = (
                Outcome.from_dict(idict['default_outcome'])
                if idict['default_outcome'] is not None else None)

            solution = (
                Solution.from_dict(idict['id'], idict['solution'])
                if idict['solution'] else {})

            state.interaction = InteractionInstance(
                idict['id'], idict['customization_args'],
                interaction_answer_groups, default_outcome,
                idict['confirmed_unclassified_answers'],
                [Fallback.from_dict(f) for f in idict['fallbacks']],
                [Hint.from_dict(h) for h in idict['hints']],
                solution)

            exploration.states[state_name] = state

        exploration.param_changes = [
            param_domain.ParamChange.from_dict(pc)
            for pc in exploration_dict['param_changes']]

        exploration.skin_instance = SkinInstance(
            feconf.DEFAULT_SKIN_ID, exploration_dict['skin_customizations'])

        exploration.version = exploration_version
        exploration.created_on = exploration_created_on
        exploration.last_updated = exploration_last_updated

        return exploration

    @classmethod
    def _require_valid_state_name(cls, name):
        """Validates name string.

        Args:
            name: str. The name to validate.
        """
        utils.require_valid_name(name, 'a state name')

    def validate(self, strict=False):
        """Validates various properties of the Exploration.

        Args:
            strict: bool. If True, the exploration is assumed to be published,
                and the validation checks are stricter.

        Raises:
            ValidationError: One or more attributes of the Exploration are
            invalid.
        """
        if not isinstance(self.title, basestring):
            raise utils.ValidationError(
                'Expected title to be a string, received %s' % self.title)
        utils.require_valid_name(
            self.title, 'the exploration title', allow_empty=True)

        if not isinstance(self.category, basestring):
            raise utils.ValidationError(
                'Expected category to be a string, received %s'
                % self.category)
        utils.require_valid_name(
            self.category, 'the exploration category', allow_empty=True)

        if not isinstance(self.objective, basestring):
            raise utils.ValidationError(
                'Expected objective to be a string, received %s' %
                self.objective)

        if not isinstance(self.language_code, basestring):
            raise utils.ValidationError(
                'Expected language_code to be a string, received %s' %
                self.language_code)
        if not any([self.language_code == lc['code']
                    for lc in constants.ALL_LANGUAGE_CODES]):
            raise utils.ValidationError(
                'Invalid language_code: %s' % self.language_code)

        if not isinstance(self.tags, list):
            raise utils.ValidationError(
                'Expected \'tags\' to be a list, received %s' % self.tags)
        for tag in self.tags:
            if not isinstance(tag, basestring):
                raise utils.ValidationError(
                    'Expected each tag in \'tags\' to be a string, received '
                    '\'%s\'' % tag)

            if not tag:
                raise utils.ValidationError('Tags should be non-empty.')

            if not re.match(feconf.TAG_REGEX, tag):
                raise utils.ValidationError(
                    'Tags should only contain lowercase letters and spaces, '
                    'received \'%s\'' % tag)

            if (tag[0] not in string.ascii_lowercase or
                    tag[-1] not in string.ascii_lowercase):
                raise utils.ValidationError(
                    'Tags should not start or end with whitespace, received '
                    ' \'%s\'' % tag)

            if re.search(r'\s\s+', tag):
                raise utils.ValidationError(
                    'Adjacent whitespace in tags should be collapsed, '
                    'received \'%s\'' % tag)
        if len(set(self.tags)) != len(self.tags):
            raise utils.ValidationError('Some tags duplicate each other')

        if not isinstance(self.blurb, basestring):
            raise utils.ValidationError(
                'Expected blurb to be a string, received %s' % self.blurb)

        if not isinstance(self.author_notes, basestring):
            raise utils.ValidationError(
                'Expected author_notes to be a string, received %s' %
                self.author_notes)

        if not isinstance(self.states, dict):
            raise utils.ValidationError(
                'Expected states to be a dict, received %s' % self.states)
        if not self.states:
            raise utils.ValidationError('This exploration has no states.')
        for state_name in self.states:
            self._require_valid_state_name(state_name)
            self.states[state_name].validate(
                self.param_specs,
                allow_null_interaction=not strict)

        if self.states_schema_version is None:
            raise utils.ValidationError(
                'This exploration has no states schema version.')
        if not self.init_state_name:
            raise utils.ValidationError(
                'This exploration has no initial state name specified.')
        if self.init_state_name not in self.states:
            raise utils.ValidationError(
                'There is no state in %s corresponding to the exploration\'s '
                'initial state name %s.' %
                (self.states.keys(), self.init_state_name))

        if not isinstance(self.param_specs, dict):
            raise utils.ValidationError(
                'Expected param_specs to be a dict, received %s'
                % self.param_specs)

        for param_name in self.param_specs:
            if not isinstance(param_name, basestring):
                raise utils.ValidationError(
                    'Expected parameter name to be a string, received %s (%s).'
                    % param_name, type(param_name))
            if not re.match(feconf.ALPHANUMERIC_REGEX, param_name):
                raise utils.ValidationError(
                    'Only parameter names with characters in [a-zA-Z0-9] are '
                    'accepted.')
            self.param_specs[param_name].validate()

        if not isinstance(self.param_changes, list):
            raise utils.ValidationError(
                'Expected param_changes to be a list, received %s'
                % self.param_changes)
        for param_change in self.param_changes:
            param_change.validate()
            if param_change.name not in self.param_specs:
                raise utils.ValidationError(
                    'No parameter named \'%s\' exists in this exploration'
                    % param_change.name)
            if param_change.name in feconf.INVALID_PARAMETER_NAMES:
                raise utils.ValidationError(
                    'The exploration-level parameter with name \'%s\' is '
                    'reserved. Please choose a different name.'
                    % param_change.name)

        # TODO(sll): Find a way to verify the param change customization args
        # when they depend on exploration/state parameters (e.g. the generated
        # values must have the correct obj_type). Can we get sample values for
        # the reader's answer and these parameters by looking at states that
        # link to this one?

        # Check that all state param changes are valid.
        for state_name, state in self.states.iteritems():
            for param_change in state.param_changes:
                param_change.validate()
                if param_change.name not in self.param_specs:
                    raise utils.ValidationError(
                        'The parameter with name \'%s\' was set in state '
                        '\'%s\', but it does not exist in the list of '
                        'parameter specifications for this exploration.'
                        % (param_change.name, state_name))
                if param_change.name in feconf.INVALID_PARAMETER_NAMES:
                    raise utils.ValidationError(
                        'The parameter name \'%s\' is reserved. Please choose '
                        'a different name for the parameter being set in '
                        'state \'%s\'.' % (param_change.name, state_name))

        # Check that all answer groups, outcomes, and param_changes are valid.
        all_state_names = self.states.keys()
        for state in self.states.values():
            interaction = state.interaction

            # Check the default destination, if any
            if (interaction.default_outcome is not None and
                    interaction.default_outcome.dest not in all_state_names):
                raise utils.ValidationError(
                    'The destination %s is not a valid state.'
                    % interaction.default_outcome.dest)

            for group in interaction.answer_groups:
                # Check group destinations.
                if group.outcome.dest not in all_state_names:
                    raise utils.ValidationError(
                        'The destination %s is not a valid state.'
                        % group.outcome.dest)

                for param_change in group.outcome.param_changes:
                    if param_change.name not in self.param_specs:
                        raise utils.ValidationError(
                            'The parameter %s was used in an answer group, '
                            'but it does not exist in this exploration'
                            % param_change.name)

        # Check that all fallbacks and hints are valid.
        for state in self.states.values():
            interaction = state.interaction

            for fallback in interaction.fallbacks:
                # Check fallback destinations.
                if fallback.outcome.dest not in all_state_names:
                    raise utils.ValidationError(
                        'The fallback destination %s is not a valid state.'
                        % fallback.outcome.dest)

                for param_change in fallback.outcome.param_changes:
                    if param_change.name not in self.param_specs:
                        raise utils.ValidationError(
                            'The parameter %s was used in a fallback, but it '
                            'does not exist in this exploration'
                            % param_change.name)

        # Check that state names required by gadgets exist.
        state_names_required_by_gadgets = set(
            self.skin_instance.get_state_names_required_by_gadgets())
        missing_state_names = state_names_required_by_gadgets - set(
            self.states.keys())
        if missing_state_names:
            raise utils.ValidationError(
                'Exploration missing required state%s: %s' % (
                    's' if len(missing_state_names) > 1 else '',
                    ', '.join(sorted(missing_state_names)))
                )

        # Check that GadgetInstances fit the skin and that gadgets are valid.
        self.skin_instance.validate()

        if strict:
            warnings_list = []

            try:
                self._verify_all_states_reachable()
            except utils.ValidationError as e:
                warnings_list.append(unicode(e))

            try:
                self._verify_no_dead_ends()
            except utils.ValidationError as e:
                warnings_list.append(unicode(e))

            if not self.title:
                warnings_list.append(
                    'A title must be specified (in the \'Settings\' tab).')

            if not self.category:
                warnings_list.append(
                    'A category must be specified (in the \'Settings\' tab).')

            if not self.objective:
                warnings_list.append(
                    'An objective must be specified (in the \'Settings\' tab).'
                )

            if not self.language_code:
                warnings_list.append(
                    'A language must be specified (in the \'Settings\' tab).')

            if len(warnings_list) > 0:
                warning_str = ''
                for ind, warning in enumerate(warnings_list):
                    warning_str += '%s. %s ' % (ind + 1, warning)
                raise utils.ValidationError(
                    'Please fix the following issues before saving this '
                    'exploration: %s' % warning_str)

    def _verify_all_states_reachable(self):
        """Verifies that all states are reachable from the initial state.

        Raises:
            ValidationError: One or more states are not reachable from the
            initial state of the Exploration.
        """
        # This queue stores state names.
        processed_queue = []
        curr_queue = [self.init_state_name]

        while curr_queue:
            curr_state_name = curr_queue[0]
            curr_queue = curr_queue[1:]

            if curr_state_name in processed_queue:
                continue

            processed_queue.append(curr_state_name)

            curr_state = self.states[curr_state_name]

            if not curr_state.interaction.is_terminal:
                all_outcomes = curr_state.interaction.get_all_outcomes()
                for outcome in all_outcomes:
                    dest_state = outcome.dest
                    if (dest_state not in curr_queue and
                            dest_state not in processed_queue):
                        curr_queue.append(dest_state)

        if len(self.states) != len(processed_queue):
            unseen_states = list(
                set(self.states.keys()) - set(processed_queue))
            raise utils.ValidationError(
                'The following states are not reachable from the initial '
                'state: %s' % ', '.join(unseen_states))

    def _verify_no_dead_ends(self):
        """Verifies that all states can reach a terminal state without using
        fallbacks.

        Raises:
            ValidationError: If is impossible to complete the exploration from
                a state.
        """
        # This queue stores state names.
        processed_queue = []
        curr_queue = []

        for (state_name, state) in self.states.iteritems():
            if state.interaction.is_terminal:
                curr_queue.append(state_name)

        while curr_queue:
            curr_state_name = curr_queue[0]
            curr_queue = curr_queue[1:]

            if curr_state_name in processed_queue:
                continue

            processed_queue.append(curr_state_name)

            for (state_name, state) in self.states.iteritems():
                if (state_name not in curr_queue
                        and state_name not in processed_queue):
                    all_outcomes = (
                        state.interaction.get_all_non_fallback_outcomes())
                    for outcome in all_outcomes:
                        if outcome.dest == curr_state_name:
                            curr_queue.append(state_name)
                            break

        if len(self.states) != len(processed_queue):
            dead_end_states = list(
                set(self.states.keys()) - set(processed_queue))
            raise utils.ValidationError(
                'It is impossible to complete the exploration from the '
                'following states: %s' % ', '.join(dead_end_states))

    # Derived attributes of an exploration,
    @property
    def init_state(self):
        """The state which forms the start of this exploration.

        Returns:
            State. The corresponding State domain object.
        """
        return self.states[self.init_state_name]

    @property
    def param_specs_dict(self):
        """A dict of param specs, each represented as Python dicts.

        Returns:
            dict. Dict of parameter specs.
        """
        return {ps_name: ps_val.to_dict()
                for (ps_name, ps_val) in self.param_specs.iteritems()}

    @property
    def param_change_dicts(self):
        """A list of param changes, represented as JSONifiable Python dicts.

        Returns:
            list(dict). List of dicts, each representing a parameter change.
        """
        return [param_change.to_dict() for param_change in self.param_changes]

    @classmethod
    def is_demo_exploration_id(cls, exploration_id):
        """Whether the given exploration id is a demo exploration.

        Args:
            exploration_id: str. The exploration id.

        Returns:
            bool. Whether the corresponding exploration is a demo exploration.
        """
        return exploration_id in feconf.DEMO_EXPLORATIONS

    @property
    def is_demo(self):
        """Whether the exploration is one of the demo explorations.

        Returns:
            bool. True is the current exploration is a demo exploration.
        """
        return self.is_demo_exploration_id(self.id)

    def update_title(self, title):
        """Update the exploration title.

        Args:
            title: str. The exploration title to set.
        """
        self.title = title

    def update_category(self, category):
        """Update the exploration category.

        Args:
            category: str. The exploration category to set.
        """
        self.category = category

    def update_objective(self, objective):
        """Update the exploration objective.

        Args:
            objective: str. The exploration objective to set.
        """
        self.objective = objective

    def update_language_code(self, language_code):
        """Update the exploration language code.

        Args:
            language_code: str. The exploration language code to set.
        """
        self.language_code = language_code

    def update_tags(self, tags):
        """Update the tags of the exploration.

        Args:
            tags: list(str). List of tags to set.
        """
        self.tags = tags

    def update_blurb(self, blurb):
        """Update the blurb of the exploration.

        Args:
            blurb: str. The blurb to set.
        """
        self.blurb = blurb

    def update_author_notes(self, author_notes):
        """Update the author notes of the exploration.

        Args:
            author_notes: str. The author notes to set.
        """
        self.author_notes = author_notes

    def update_param_specs(self, param_specs_dict):
        """Update the param spec dict.

        Args:
            param_specs_dict: dict. A dict where each key-value pair represents
                respectively, a param spec name and a dict used to initialize a
                ParamSpec domain object.
        """
        self.param_specs = {
            ps_name: param_domain.ParamSpec.from_dict(ps_val)
            for (ps_name, ps_val) in param_specs_dict.iteritems()
        }

    def update_param_changes(self, param_changes_list):
        """Update the param change dict.

        Args:
           param_changes_list: list(dict). List of dict where each dict is
                used to initialize a ParamChange domain object.
        """
        self.param_changes = [
            param_domain.ParamChange.from_dict(param_change)
            for param_change in param_changes_list
        ]

    def update_init_state_name(self, init_state_name):
        """Update the name for the initial state of the exploration.

        Args:
            init_state_name: str. The new name of the initial state.
        """
        if init_state_name not in self.states:
            raise Exception(
                'Invalid new initial state name: %s; '
                'it is not in the list of states %s for this '
                'exploration.' % (init_state_name, self.states.keys()))
        self.init_state_name = init_state_name

    # Methods relating to states.
    def add_states(self, state_names):
        """Adds multiple states to the exploration.

        Args:
            state_names: list(str). List of state names to add.

        Raises:
            ValueError: At least one of the new state names already exists in
            the states dict.
        """
        for state_name in state_names:
            if state_name in self.states:
                raise ValueError('Duplicate state name %s' % state_name)

        for state_name in state_names:
            self.states[state_name] = State.create_default_state(state_name)

    def rename_state(self, old_state_name, new_state_name):
        """Renames the given state.

        Args:
            old_state_names: str. The old name of state to rename.
            new_state_names: str. The new state name.

        Raises:
            ValueError: The old state name does not exist or the new state name
            is already in states dict.
        """
        if old_state_name not in self.states:
            raise ValueError('State %s does not exist' % old_state_name)
        if (old_state_name != new_state_name and
                new_state_name in self.states):
            raise ValueError('Duplicate state name: %s' % new_state_name)

        if old_state_name == new_state_name:
            return

        self._require_valid_state_name(new_state_name)

        self.states[new_state_name] = copy.deepcopy(
            self.states[old_state_name])
        del self.states[old_state_name]

        if self.init_state_name == old_state_name:
            self.update_init_state_name(new_state_name)

        # Find all destinations in the exploration which equal the renamed
        # state, and change the name appropriately.
        for other_state_name in self.states:
            other_state = self.states[other_state_name]
            other_outcomes = other_state.interaction.get_all_outcomes()
            for outcome in other_outcomes:
                if outcome.dest == old_state_name:
                    outcome.dest = new_state_name

    def delete_state(self, state_name):
        """Deletes the given state.

        Args:
            state_names: str. The state name to be deleted.

        Raises:
            ValueError: The state does not exist or is the initial state of the
            exploration.
        """
        if state_name not in self.states:
            raise ValueError('State %s does not exist' % state_name)

        # Do not allow deletion of initial states.
        if self.init_state_name == state_name:
            raise ValueError('Cannot delete initial state of an exploration.')

        # Find all destinations in the exploration which equal the deleted
        # state, and change them to loop back to their containing state.
        for other_state_name in self.states:
            other_state = self.states[other_state_name]
            all_outcomes = other_state.interaction.get_all_outcomes()
            for outcome in all_outcomes:
                if outcome.dest == state_name:
                    outcome.dest = other_state_name

        del self.states[state_name]

    # Methods relating to gadgets.
    def add_gadget(self, gadget_dict, panel):
        """Adds a gadget to the associated panel.

        Args:
            gadget_dict: dict. The dict representation of GadgetInstance
                object.
            panel: str. The panel name.
        """
        gadget_instance = GadgetInstance(
            gadget_dict['gadget_type'], gadget_dict['gadget_name'],
            gadget_dict['visible_in_states'],
            gadget_dict['customization_args'])

        self.skin_instance.panel_contents_dict[panel].append(
            gadget_instance)

    def rename_gadget(self, old_gadget_name, new_gadget_name):
        """Renames the given gadget.

        Args:
            old_gadget_name: str. The old name of gadget to rename.
            new_gadget_name: str. The new gadget name.

        Raises:
            ValueError: The old gadget name does not exist or the new gadget
            name already exists.
        """
        if old_gadget_name not in self.get_all_gadget_names():
            raise ValueError('Gadget %s does not exist.' % old_gadget_name)
        if (old_gadget_name != new_gadget_name and
                new_gadget_name in self.get_all_gadget_names()):
            raise ValueError('Duplicate gadget name: %s' % new_gadget_name)

        if old_gadget_name == new_gadget_name:
            return

        GadgetInstance._validate_gadget_name(new_gadget_name)  # pylint: disable=protected-access

        gadget_instance = self.get_gadget_instance_by_name(old_gadget_name)
        gadget_instance.name = new_gadget_name

    def delete_gadget(self, gadget_name):
        """Deletes the given gadget.

        Args:
            gadget_name: str. The name of the gadget to be deleted.

        Raises:
            ValueError: The gadget does not exist.
        """
        if gadget_name not in self.get_all_gadget_names():
            raise ValueError('Gadget %s does not exist.' % gadget_name)

        panel = self._get_panel_for_gadget(gadget_name)
        gadget_index = None
        for index in range(len(
                self.skin_instance.panel_contents_dict[panel])):
            if self.skin_instance.panel_contents_dict[
                    panel][index].name == gadget_name:
                gadget_index = index
                break
        del self.skin_instance.panel_contents_dict[panel][gadget_index]

    def get_gadget_instance_by_name(self, gadget_name):
        """Returns the GadgetInstance with the given name.

        Args:
            gadget_name: str. The gadget name.

        Returns:
            GadgetInstance. The corresponding GadgetInstance domain object.

        Raises:
            ValueError: The gadget does not exist.
        """
        for gadget_instances in (
                self.skin_instance.panel_contents_dict.itervalues()):
            for gadget_instance in gadget_instances:
                if gadget_instance.name == gadget_name:
                    return gadget_instance
        raise ValueError('Gadget %s does not exist.' % gadget_name)

    def get_all_gadget_names(self):
        """Gets a list of names of all gadgets used in this exploration.

        Returns:
            list(GadgetInstance). The list of all gadget names.
        """
        gadget_names = set()
        for gadget_instances in (
                self.skin_instance.panel_contents_dict.itervalues()):
            for gadget_instance in gadget_instances:
                gadget_names.add(gadget_instance.name)
        return sorted(gadget_names)

    def _get_panel_for_gadget(self, gadget_name):
        """Returns the panel name for the given GadgetInstance.

        Args:
            gadget_name: str. The gadget name.

        Returns:
            str. The corresponding panel name.

        Raises:
            ValueError: The gadget does not exist.
        """
        for panel, gadget_instances in (
                self.skin_instance.panel_contents_dict.iteritems()):
            for gadget_instance in gadget_instances:
                if gadget_instance.name == gadget_name:
                    return panel
        raise ValueError('Gadget %s does not exist.' % gadget_name)

    def _update_gadget_visibilities_for_renamed_state(
            self, old_state_name, new_state_name):
        """Updates the visible_in_states property for gadget instances
        visible in the renamed state.

        Args:
            old_state_name: str. The name of state to remove.
            new_state_name: str. The new gadget name to append in
                'visible_in_states' list.
        """
        affected_gadget_instances = (
            self._get_gadget_instances_visible_in_state(old_state_name))

        for gadget_instance in affected_gadget_instances:
            # Order within visible_in_states does not affect functionality.
            # It's sorted purely for deterministic testing.
            gadget_instance.visible_in_states.remove(old_state_name)
            gadget_instance.visible_in_states.append(new_state_name)
            gadget_instance.visible_in_states.sort()

    def _update_gadget_visibilities_for_deleted_state(self, state_name):
        """Updates the visible_in_states property for gadget instances
        visible in the deleted state.

        Args:
            state_name: str. The state name.
        """
        affected_gadget_instances = (
            self._get_gadget_instances_visible_in_state(state_name))

        for gadget_instance in affected_gadget_instances:
            gadget_instance.visible_in_states.remove(state_name)
            if len(gadget_instance.visible_in_states) == 0:
                raise utils.ValidationError(
                    "Deleting '%s' state leaves '%s' gadget with no visible "
                    'states. This is not currently supported and should be '
                    'handled with editor guidance on the front-end.' % (
                        state_name,
                        gadget_instance.name)
                )

    def _get_gadget_instances_visible_in_state(self, state_name):
        """Helper function to retrieve gadget instances visible in
        a given state.

        Args:
            state_name: str. The name of state in which search for gadgets.

        Returns:
            list(GadgetInstance). List of gadgets visibile in a givent state.
        """
        visible_gadget_instances = []
        for gadget_instances in (
                self.skin_instance.panel_contents_dict.itervalues()):
            for gadget_instance in gadget_instances:
                if state_name in gadget_instance.visible_in_states:
                    visible_gadget_instances.append(gadget_instance)
        return visible_gadget_instances

    @classmethod
    def _convert_states_v0_dict_to_v1_dict(cls, states_dict):
        """Converts old states schema to the modern v1 schema. v1 contains the
        schema version 1 and does not contain any old constructs, such as
        widgets. This is a complete migration of everything previous to the
        schema versioning update to the earliest versioned schema.
        Note that the states_dict being passed in is modified in-place.

        Args:
            states_dict: dict. A dict where each key-value pair represents,
                respectively, a state name and a dict used to initialize a
                State domain object.

        Returns:
            dict. The converted states_dict.
        """
        # ensure widgets are renamed to be interactions
        for _, state_defn in states_dict.iteritems():
            if 'widget' not in state_defn:
                continue
            state_defn['interaction'] = copy.deepcopy(state_defn['widget'])
            state_defn['interaction']['id'] = copy.deepcopy(
                state_defn['interaction']['widget_id'])
            del state_defn['interaction']['widget_id']
            if 'sticky' in state_defn['interaction']:
                del state_defn['interaction']['sticky']
            del state_defn['widget']
        return states_dict

    @classmethod
    def _convert_states_v1_dict_to_v2_dict(cls, states_dict):
        """Converts from version 1 to 2. Version 1 assumes the existence of an
        implicit 'END' state, but version 2 does not. As a result, the
        conversion process involves introducing a proper ending state for all
        explorations previously designed under this assumption.
        Note that the states_dict being passed in is modified in-place.

        Args:
            states_dict: dict. A dict where each key-value pair represents,
                respectively, a state name and a dict used to initialize a
                State domain object.

        Returns:
            dict. The converted states_dict.
        """
        # The name of the implicit END state before the migration. Needed here
        # to migrate old explorations which expect that implicit END state.
        old_end_dest = 'END'

        # Adds an explicit state called 'END' with an EndExploration to replace
        # links other states have to an implicit 'END' state. Otherwise, if no
        # states refer to a state called 'END', no new state will be introduced
        # since it would be isolated from all other states in the graph and
        # create additional warnings for the user. If they were not referring
        # to an 'END' state before, then they would only be receiving warnings
        # about not being able to complete the exploration. The introduction of
        # a real END state would produce additional warnings (state cannot be
        # reached from other states, etc.)
        targets_end_state = False
        has_end_state = False
        for (state_name, sdict) in states_dict.iteritems():
            if not has_end_state and state_name == old_end_dest:
                has_end_state = True

            if not targets_end_state:
                for handler in sdict['interaction']['handlers']:
                    for rule_spec in handler['rule_specs']:
                        if rule_spec['dest'] == old_end_dest:
                            targets_end_state = True
                            break

        # Ensure any explorations pointing to an END state has a valid END
        # state to end with (in case it expects an END state)
        if targets_end_state and not has_end_state:
            states_dict[old_end_dest] = {
                'content': [{
                    'type': 'text',
                    'value': 'Congratulations, you have finished!'
                }],
                'interaction': {
                    'id': 'EndExploration',
                    'customization_args': {
                        'recommendedExplorationIds': {
                            'value': []
                        }
                    },
                    'handlers': [{
                        'name': 'submit',
                        'rule_specs': [{
                            'definition': {
                                'rule_type': 'default'
                            },
                            'dest': old_end_dest,
                            'feedback': [],
                            'param_changes': []
                        }]
                    }],
                },
                'param_changes': []
            }

        return states_dict

    @classmethod
    def _convert_states_v2_dict_to_v3_dict(cls, states_dict):
        """Converts from version 2 to 3. Version 3 introduces a triggers list
        within interactions.
        Note that the states_dict being passed in is modified in-place.

        Args:
            states_dict: dict. A dict where each key-value pair represents,
                respectively, a state name and a dict used to initialize a
                State domain object.

        Returns:
            dict. The converted states_dict.
        """
        # Ensure all states interactions have a triggers list.
        for sdict in states_dict.values():
            interaction = sdict['interaction']
            if 'triggers' not in interaction:
                interaction['triggers'] = []

        return states_dict

    @classmethod
    def _convert_states_v3_dict_to_v4_dict(cls, states_dict):
        """Converts from version 3 to 4. Version 4 introduces a new structure
        for rules by organizing them into answer groups instead of handlers.
        This migration involves a 1:1 mapping from rule specs to answer groups
        containing just that single rule. Default rules have their destination
        state name and feedback copied to the default_outcome portion of an
        interaction instance.
        Note that the states_dict being passed in is modified in-place.

        Args:
            states_dict: dict. A dict where each key-value pair represents,
                respectively, a state name and a dict used to initialize a
                State domain object.

        Returns:
            dict. The converted states_dict.
        """
        for state_dict in states_dict.values():
            interaction = state_dict['interaction']
            answer_groups = []
            default_outcome = None
            for handler in interaction['handlers']:
                # Ensure the name is 'submit'.
                if 'name' in handler and handler['name'] != 'submit':
                    raise utils.ExplorationConversionError(
                        'Error: Can only convert rules with a name '
                        '\'submit\' in states v3 to v4 conversion process. '
                        'Encountered name: %s' % handler['name'])

                # Each rule spec becomes a new answer group.
                for rule_spec in handler['rule_specs']:
                    group = {}

                    # Rules don't have a rule_type key anymore.
                    is_default_rule = False
                    if 'rule_type' in rule_spec['definition']:
                        rule_type = rule_spec['definition']['rule_type']
                        is_default_rule = (rule_type == 'default')

                        # Ensure the rule type is either default or atomic.
                        if not is_default_rule and rule_type != 'atomic':
                            raise utils.ExplorationConversionError(
                                'Error: Can only convert default and atomic '
                                'rules in states v3 to v4 conversion process. '
                                'Encountered rule of type: %s' % rule_type)

                    # Ensure the subject is answer.
                    if ('subject' in rule_spec['definition'] and
                            rule_spec['definition']['subject'] != 'answer'):
                        raise utils.ExplorationConversionError(
                            'Error: Can only convert rules with an \'answer\' '
                            'subject in states v3 to v4 conversion process. '
                            'Encountered subject: %s'
                            % rule_spec['definition']['subject'])

                    # The rule turns into the group's only rule. Rules do not
                    # have definitions anymore. Do not copy the inputs and name
                    # if it is a default rule.
                    if not is_default_rule:
                        definition = rule_spec['definition']
                        group['rule_specs'] = [{
                            'inputs': copy.deepcopy(definition['inputs']),
                            'rule_type': copy.deepcopy(definition['name'])
                        }]

                    # Answer groups now have an outcome.
                    group['outcome'] = {
                        'dest': copy.deepcopy(rule_spec['dest']),
                        'feedback': copy.deepcopy(rule_spec['feedback']),
                        'param_changes': (
                            copy.deepcopy(rule_spec['param_changes'])
                            if 'param_changes' in rule_spec else [])
                    }

                    if is_default_rule:
                        default_outcome = group['outcome']
                    else:
                        answer_groups.append(group)

            try:
                is_terminal = (
                    interaction_registry.Registry.get_interaction_by_id(
                        interaction['id']
                    ).is_terminal if interaction['id'] is not None else False)
            except KeyError:
                raise utils.ExplorationConversionError(
                    'Trying to migrate exploration containing non-existent '
                    'interaction ID: %s' % interaction['id'])
            if not is_terminal:
                interaction['answer_groups'] = answer_groups
                interaction['default_outcome'] = default_outcome
            else:
                # Terminal nodes have no answer groups or outcomes.
                interaction['answer_groups'] = []
                interaction['default_outcome'] = None
            del interaction['handlers']

        return states_dict

    @classmethod
    def _convert_states_v4_dict_to_v5_dict(cls, states_dict):
        """Converts from version 4 to 5. Version 5 removes the triggers list
        within interactions, and replaces it with a fallbacks list.
        Note that the states_dict being passed in is modified in-place.

        Args:
            states_dict: dict. A dict where each key-value pair represents,
                respectively, a state name and a dict used to initialize a
                State domain object.

        Returns:
            dict. The converted states_dict.
        """
        # Ensure all states interactions have a fallbacks list.
        for state_dict in states_dict.values():
            interaction = state_dict['interaction']
            if 'triggers' in interaction:
                del interaction['triggers']
            if 'fallbacks' not in interaction:
                interaction['fallbacks'] = []

        return states_dict

    @classmethod
    def _convert_states_v5_dict_to_v6_dict(cls, states_dict):
        """Converts from version 5 to 6. Version 6 introduces a list of
        confirmed unclassified answers. Those are answers which are confirmed
        to be associated with the default outcome during classification.

        Args:
            states_dict: dict. A dict where each key-value pair represents,
                respectively, a state name and a dict used to initialize a
                State domain object.

        Returns:
            dict. The converted states_dict.
        """
        for state_dict in states_dict.values():
            interaction = state_dict['interaction']
            if 'confirmed_unclassified_answers' not in interaction:
                interaction['confirmed_unclassified_answers'] = []

        return states_dict

    @classmethod
    def _convert_states_v6_dict_to_v7_dict(cls, states_dict):
        """Converts from version 6 to 7. Version 7 forces all CodeRepl
        interactions to use Python.

        Args:
            states_dict: dict. A dict where each key-value pair represents,
                respectively, a state name and a dict used to initialize a
                State domain object.

        Returns:
            dict. The converted states_dict.
        """
        for state_dict in states_dict.values():
            interaction = state_dict['interaction']
            if interaction['id'] == 'CodeRepl':
                interaction['customization_args']['language']['value'] = (
                    'python')

        return states_dict

    # TODO(bhenning): Remove pre_v4_states_conversion_func when the answer
    # migration is completed.
    @classmethod
    def _convert_states_v7_dict_to_v8_dict(cls, states_dict):
        """Converts from version 7 to 8. Version 8 contains classifier
        model id.

        Args:
            states_dict: dict. A dict where each key-value pair represents,
                respectively, a state name and a dict used to initialize a
                State domain object.

        Returns:
            dict. The converted states_dict.
        """
        for state_dict in states_dict.values():
            state_dict['classifier_model_id'] = None
        return states_dict

    @classmethod
    def _convert_states_v8_dict_to_v9_dict(cls, states_dict):
        """Converts from version 8 to 9. Version 9 contains 'correct'
        field in answer groups.

        Args:
            states_dict: dict. A dict where each key-value pair represents,
                respectively, a state name and a dict used to initialize a
                State domain object.

        Returns:
            dict. The converted states_dict.
        """
        for state_dict in states_dict.values():
            answer_groups = state_dict['interaction']['answer_groups']
            for answer_group in answer_groups:
                answer_group['correct'] = False
        return states_dict

    @classmethod
    def _convert_states_v9_dict_to_v10_dict(cls, states_dict):
        """Converts from version 9 to 10. Version 10 contains hints
        and solution in each interaction.

        Args:
            states_dict: dict. A dict where each key-value pair represents,
                respectively, a state name and a dict used to initialize a
                State domain object.

        Returns:
            dict. The converted states_dict.
        """
        for state_dict in states_dict.values():
            interaction = state_dict['interaction']
            if 'hints' not in interaction:
                interaction['hints'] = []
                for fallback in interaction['fallbacks']:
                    if fallback['outcome']['feedback']:
                        interaction['hints'].append(
                            Hint(fallback['outcome']['feedback'][0]).to_dict())
            if 'solution' not in interaction:
                interaction['solution'] = {}
        return states_dict

    @classmethod
    def _convert_states_v10_dict_to_v11_dict(cls, states_dict):
        """Converts from version 10 to 11. Version 11 refactors the content to
        be an HTML string with audio translations.
        """
        for state_dict in states_dict.values():
            content_html = state_dict['content'][0]['value']
            state_dict['content'] = {
                'html': content_html,
                'audio_translations': []
            }
        return states_dict

    @classmethod
    def _convert_states_v11_dict_to_v12_dict(cls, states_dict):
        """Converts from version 11 to 12. Version 12 refactors audio
        translations from a list to a dict keyed by language code.
        """
        for state_dict in states_dict.values():
            old_audio_translations = state_dict['content']['audio_translations']
            state_dict['content']['audio_translations'] = {
                old_translation['language_code']: {
                    'filename': old_translation['filename'],
                    'file_size_bytes': old_translation['file_size_bytes'],
                    'needs_update': old_translation['needs_update'],
                }
                for old_translation in old_audio_translations
            }
        return states_dict

    @classmethod
    def update_states_from_model(
            cls, versioned_exploration_states, current_states_schema_version):
        """Converts the states blob contained in the given
        versioned_exploration_states dict from current_states_schema_version to
        current_states_schema_version + 1.
        Note that the versioned_exploration_states being passed in is modified
        in-place.

        Args:
            versioned_exploration_states: dict. A dict with two keys:
                - states_schema_version: int. The states schema version for the
                    exploration.
                - states: dict. The dict of states comprising the exploration.
                    The keys are state names and the values are dicts used to
                    initialize a State domain object.
            current_states_schema_version: int. The current states
                schema version.
        """
        versioned_exploration_states['states_schema_version'] = (
            current_states_schema_version + 1)

        conversion_fn = getattr(cls, '_convert_states_v%s_dict_to_v%s_dict' % (
            current_states_schema_version, current_states_schema_version + 1))
        versioned_exploration_states['states'] = conversion_fn(
            versioned_exploration_states['states'])

    # The current version of the exploration YAML schema. If any backward-
    # incompatible changes are made to the exploration schema in the YAML
    # definitions, this version number must be changed and a migration process
    # put in place.
    CURRENT_EXP_SCHEMA_VERSION = 15
    LAST_UNTITLED_SCHEMA_VERSION = 9

    @classmethod
    def _convert_v1_dict_to_v2_dict(cls, exploration_dict):
        """Converts a v1 exploration dict into a v2 exploration dict.

        Args:
            exploration_dict: dict. The dict representation of an exploration
                with schema version v1.

        Returns:
            dict. The dict representation of the Exploration domain object,
            following schema version v2.
        """
        exploration_dict['schema_version'] = 2
        exploration_dict['init_state_name'] = (
            exploration_dict['states'][0]['name'])

        states_dict = {}
        for state in exploration_dict['states']:
            states_dict[state['name']] = state
            del states_dict[state['name']]['name']
        exploration_dict['states'] = states_dict

        return exploration_dict

    @classmethod
    def _convert_v2_dict_to_v3_dict(cls, exploration_dict):
        """Converts a v2 exploration dict into a v3 exploration dict.

        Args:
            exploration_dict: dict. The dict representation of an exploration
                with schema version v2.

        Returns:
            dict. The dict representation of the Exploration domain object,
            following schema version v3.
        """
        exploration_dict['schema_version'] = 3

        exploration_dict['objective'] = ''
        exploration_dict['language_code'] = constants.DEFAULT_LANGUAGE_CODE
        exploration_dict['skill_tags'] = []
        exploration_dict['blurb'] = ''
        exploration_dict['author_notes'] = ''

        return exploration_dict

    @classmethod
    def _convert_v3_dict_to_v4_dict(cls, exploration_dict):
        """Converts a v3 exploration dict into a v4 exploration dict.

        Args:
            exploration_dict: dict. The dict representation of an exploration
                with schema version v3.

        Returns:
            dict. The dict representation of the Exploration domain object,
            following schema version v4.
        """
        exploration_dict['schema_version'] = 4

        for _, state_defn in exploration_dict['states'].iteritems():
            state_defn['interaction'] = copy.deepcopy(state_defn['widget'])
            state_defn['interaction']['id'] = copy.deepcopy(
                state_defn['interaction']['widget_id'])
            del state_defn['interaction']['widget_id']
            del state_defn['interaction']['sticky']
            del state_defn['widget']

        return exploration_dict

    @classmethod
    def _convert_v4_dict_to_v5_dict(cls, exploration_dict):
        """Converts a v4 exploration dict into a v5 exploration dict.

        Args:
            exploration_dict: dict. The dict representation of an exploration
                with schema version v4.

        Returns:
            dict. The dict representation of the Exploration domain object,
            following schema version v5.
        """
        exploration_dict['schema_version'] = 5

        # Rename the 'skill_tags' field to 'tags'.
        exploration_dict['tags'] = exploration_dict['skill_tags']
        del exploration_dict['skill_tags']

        exploration_dict['skin_customizations'] = {
            'panels_contents': {
                'bottom': [],
                'left': [],
                'right': []
            }
        }

        return exploration_dict

    @classmethod
    def _convert_v5_dict_to_v6_dict(cls, exploration_dict):
        """Converts a v5 exploration dict into a v6 exploration dict.

        Args:
            exploration_dict: dict. The dict representation of an exploration
                with schema version v5.

        Returns:
            dict. The dict representation of the Exploration domain object,
            following schema version v6.
        """
        exploration_dict['schema_version'] = 6

        # Ensure this exploration is up-to-date with states schema v3.
        exploration_dict['states'] = cls._convert_states_v0_dict_to_v1_dict(
            exploration_dict['states'])
        exploration_dict['states'] = cls._convert_states_v1_dict_to_v2_dict(
            exploration_dict['states'])
        exploration_dict['states'] = cls._convert_states_v2_dict_to_v3_dict(
            exploration_dict['states'])

        # Update the states schema version to reflect the above conversions to
        # the states dict.
        exploration_dict['states_schema_version'] = 3

        return exploration_dict

    @classmethod
    def _convert_v6_dict_to_v7_dict(cls, exploration_dict):
        """Converts a v6 exploration dict into a v7 exploration dict.

        Args:
            exploration_dict: dict. The dict representation of an exploration
                with schema version v6.

        Returns:
            dict. The dict representation of the Exploration domain object,
            following schema version v7.
        """
        exploration_dict['schema_version'] = 7

        # Ensure this exploration is up-to-date with states schema v4.
        exploration_dict['states'] = cls._convert_states_v3_dict_to_v4_dict(
            exploration_dict['states'])

        # Update the states schema version to reflect the above conversions to
        # the states dict.
        exploration_dict['states_schema_version'] = 4

        return exploration_dict

    @classmethod
    def _convert_v7_dict_to_v8_dict(cls, exploration_dict):
        """Converts a v7 exploration dict into a v8 exploration dict.

        Args:
            exploration_dict: dict. The dict representation of an exploration
                with schema version v7.

        Returns:
            dict. The dict representation of the Exploration domain object,
            following schema version v8.
        """
        exploration_dict['schema_version'] = 8

        # Ensure this exploration is up-to-date with states schema v5.
        exploration_dict['states'] = cls._convert_states_v4_dict_to_v5_dict(
            exploration_dict['states'])

        # Update the states schema version to reflect the above conversions to
        # the states dict.
        exploration_dict['states_schema_version'] = 5

        return exploration_dict

    @classmethod
    def _convert_v8_dict_to_v9_dict(cls, exploration_dict):
        """Converts a v8 exploration dict into a v9 exploration dict.

        Args:
            exploration_dict: dict. The dict representation of an exploration
                with schema version v8.

        Returns:
            dict. The dict representation of the Exploration domain object,
            following schema version v9.
        """
        exploration_dict['schema_version'] = 9

        # Ensure this exploration is up-to-date with states schema v6.
        exploration_dict['states'] = cls._convert_states_v5_dict_to_v6_dict(
            exploration_dict['states'])

        # Update the states schema version to reflect the above conversions to
        # the states dict.
        exploration_dict['states_schema_version'] = 6

        return exploration_dict

    @classmethod
    def _convert_v9_dict_to_v10_dict(cls, exploration_dict, title, category):
        """Converts a v9 exploration dict into a v10 exploration dict.

        Args:
            exploration_dict: dict. The dict representation of an exploration
                with schema version v9.
            title: str. The exploration title.
            category: str. The exploration category.

        Returns:
            dict. The dict representation of the Exploration domain object,
            following schema version v10.
        """

        exploration_dict['schema_version'] = 10

        # From v10 onwards, the title and schema version are stored in the YAML
        # file.
        exploration_dict['title'] = title
        exploration_dict['category'] = category

        # Remove the 'default_skin' property.
        del exploration_dict['default_skin']

        # Upgrade all gadget panel customizations to have exactly one empty
        # bottom panel. This is fine because, for previous schema versions,
        # gadgets functionality had not been released yet.
        exploration_dict['skin_customizations'] = {
            'panels_contents': {
                'bottom': [],
            }
        }

        # Ensure this exploration is up-to-date with states schema v7.
        exploration_dict['states'] = cls._convert_states_v6_dict_to_v7_dict(
            exploration_dict['states'])

        # Update the states schema version to reflect the above conversions to
        # the states dict.
        exploration_dict['states_schema_version'] = 7

        return exploration_dict

    @classmethod
    def _convert_v10_dict_to_v11_dict(cls, exploration_dict):
        """Converts a v10 exploration dict into a v11 exploration dict.

        Args:
            exploration_dict: dict. The dict representation of an exploration
                with schema version v10.

        Returns:
            dict. The dict representation of the Exploration domain object,
            following schema version v11.
        """

        exploration_dict['schema_version'] = 11

        exploration_dict['states'] = cls._convert_states_v7_dict_to_v8_dict(
            exploration_dict['states'])

        exploration_dict['states_schema_version'] = 8

        return exploration_dict

    @classmethod
    def _convert_v11_dict_to_v12_dict(cls, exploration_dict):
        """Converts a v11 exploration dict into a v12 exploration dict.

        Args:
            exploration_dict: dict. The dict representation of an exploration
                with schema version v11.

        Returns:
            dict. The dict representation of the Exploration domain object,
            following schema version v12.
        """

        exploration_dict['schema_version'] = 12

        exploration_dict['states'] = cls._convert_states_v8_dict_to_v9_dict(
            exploration_dict['states'])

        exploration_dict['states_schema_version'] = 9

        return exploration_dict

    @classmethod
    def _convert_v12_dict_to_v13_dict(cls, exploration_dict):
        """Converts a v12 exploration dict into a v13 exploration dict."""

        exploration_dict['schema_version'] = 13

        exploration_dict['states'] = cls._convert_states_v9_dict_to_v10_dict(
            exploration_dict['states'])

        exploration_dict['states_schema_version'] = 10

        return exploration_dict

    @classmethod
    def _convert_v13_dict_to_v14_dict(cls, exploration_dict):
        """Converts a v13 exploration dict into a v14 exploration dict."""

        exploration_dict['schema_version'] = 14

        exploration_dict['states'] = cls._convert_states_v10_dict_to_v11_dict(
            exploration_dict['states'])

        exploration_dict['states_schema_version'] = 11

        return exploration_dict

    @classmethod
    def _convert_v14_dict_to_v15_dict(cls, exploration_dict):
        """Converts a v14 exploration dict into a v15 exploration dict."""

        exploration_dict['schema_version'] = 15

        exploration_dict['states'] = cls._convert_states_v11_dict_to_v12_dict(
            exploration_dict['states'])

        exploration_dict['states_schema_version'] = 12

        return exploration_dict

    @classmethod
    def _migrate_to_latest_yaml_version(
            cls, yaml_content, title=None, category=None):
        """Return the YAML content of the exploration in the latest schema
        format.

        Args:
            yaml_content: str. The YAML representation of the exploration.
            title: str. The exploration title.
            category: str. The exploration category.

        Returns:
            tuple(dict, int). The dict 'exploration_dict' is the representation
            of the Exploration and the 'initial_schema_version' is the initial
            schema version provided in 'yaml_content'.

        Raises:
            Exception: 'yaml_content' or the exploration schema version is not
                valid.
        """
        try:
            exploration_dict = utils.dict_from_yaml(yaml_content)
        except Exception as e:
            raise Exception(
                'Please ensure that you are uploading a YAML text file, not '
                'a zip file. The YAML parser returned the following error: %s'
                % e)

        exploration_schema_version = exploration_dict.get('schema_version')
        initial_schema_version = exploration_schema_version
        if exploration_schema_version is None:
            raise Exception('Invalid YAML file: no schema version specified.')
        if not (1 <= exploration_schema_version
                <= cls.CURRENT_EXP_SCHEMA_VERSION):
            raise Exception(
                'Sorry, we can only process v1 to v%s exploration YAML files '
                'at present.' % cls.CURRENT_EXP_SCHEMA_VERSION)
        if exploration_schema_version == 1:
            exploration_dict = cls._convert_v1_dict_to_v2_dict(
                exploration_dict)
            exploration_schema_version = 2

        if exploration_schema_version == 2:
            exploration_dict = cls._convert_v2_dict_to_v3_dict(
                exploration_dict)
            exploration_schema_version = 3

        if exploration_schema_version == 3:
            exploration_dict = cls._convert_v3_dict_to_v4_dict(
                exploration_dict)
            exploration_schema_version = 4

        if exploration_schema_version == 4:
            exploration_dict = cls._convert_v4_dict_to_v5_dict(
                exploration_dict)
            exploration_schema_version = 5

        if exploration_schema_version == 5:
            exploration_dict = cls._convert_v5_dict_to_v6_dict(
                exploration_dict)
            exploration_schema_version = 6

        if exploration_schema_version == 6:
            exploration_dict = cls._convert_v6_dict_to_v7_dict(
                exploration_dict)
            exploration_schema_version = 7

        if exploration_schema_version == 7:
            exploration_dict = cls._convert_v7_dict_to_v8_dict(
                exploration_dict)
            exploration_schema_version = 8

        if exploration_schema_version == 8:
            exploration_dict = cls._convert_v8_dict_to_v9_dict(
                exploration_dict)
            exploration_schema_version = 9

        if exploration_schema_version == 9:
            exploration_dict = cls._convert_v9_dict_to_v10_dict(
                exploration_dict, title, category)
            exploration_schema_version = 10

        if exploration_schema_version == 10:
            exploration_dict = cls._convert_v10_dict_to_v11_dict(
                exploration_dict)
            exploration_schema_version = 11

        if exploration_schema_version == 11:
            exploration_dict = cls._convert_v11_dict_to_v12_dict(
                exploration_dict)
            exploration_schema_version = 12

        if exploration_schema_version == 12:
            exploration_dict = cls._convert_v12_dict_to_v13_dict(
                exploration_dict)
            exploration_schema_version = 13

        if exploration_schema_version == 13:
            exploration_dict = cls._convert_v13_dict_to_v14_dict(
                exploration_dict)
            exploration_schema_version = 14

        if exploration_schema_version == 14:
            exploration_dict = cls._convert_v14_dict_to_v15_dict(
                exploration_dict)
            exploration_schema_version = 15

        return (exploration_dict, initial_schema_version)

    @classmethod
    def from_yaml(cls, exploration_id, yaml_content):
        """Creates and returns exploration from a YAML text string for YAML
        schema versions 10 and later.

        Args:
            exploration_id: str. The id of the exploration.
            yaml_content: str. The YAML representation of the exploration.

        Returns:
            Exploration. The corresponding exploration domain object.

        Raises:
            Exception: The initial schema version of exploration is less than
                or equal to 9.
        """
        migration_result = cls._migrate_to_latest_yaml_version(yaml_content)
        exploration_dict = migration_result[0]
        initial_schema_version = migration_result[1]

        if (initial_schema_version <=
                cls.LAST_UNTITLED_SCHEMA_VERSION):
            raise Exception(
                'Expected a YAML version >= 10, received: %d' % (
                    initial_schema_version))

        exploration_dict['id'] = exploration_id
        return Exploration.from_dict(exploration_dict)

    @classmethod
    def from_untitled_yaml(cls, exploration_id, title, category, yaml_content):
        """Creates and returns exploration from a YAML text string. This is
        for importing explorations using YAML schema version 9 or earlier.

        Args:
            exploration_id: str. The id of the exploration.
            title: str. The exploration title.
            category: str. The exploration category.
            yaml_content: str. The YAML representation of the exploration.

        Returns:
            Exploration. The corresponding exploration domain object.

        Raises:
            Exception: The initial schema version of exploration is less than
                or equal to 9.
        """
        migration_result = cls._migrate_to_latest_yaml_version(
            yaml_content, title, category)
        exploration_dict = migration_result[0]
        initial_schema_version = migration_result[1]

        if (initial_schema_version >
                cls.LAST_UNTITLED_SCHEMA_VERSION):
            raise Exception(
                'Expected a YAML version <= 9, received: %d' % (
                    initial_schema_version))

        exploration_dict['id'] = exploration_id
        return Exploration.from_dict(exploration_dict)

    def to_yaml(self):
        """Convert the exploration domain object into YAML string.

        Returns:
            str. The YAML representation of this exploration.
        """
        exp_dict = self.to_dict()
        exp_dict['schema_version'] = self.CURRENT_EXP_SCHEMA_VERSION

        # The ID is the only property which should not be stored within the
        # YAML representation.
        del exp_dict['id']

        return utils.yaml_from_dict(exp_dict)

    def to_dict(self):
        """Returns a copy of the exploration as a dictionary. It includes all
        necessary information to represent the exploration.

        Returns:
            dict. A dict mapping all fields of Exploration instance.
        """
        return copy.deepcopy({
            'id': self.id,
            'title': self.title,
            'category': self.category,
            'author_notes': self.author_notes,
            'blurb': self.blurb,
            'states_schema_version': self.states_schema_version,
            'init_state_name': self.init_state_name,
            'language_code': self.language_code,
            'objective': self.objective,
            'param_changes': self.param_change_dicts,
            'param_specs': self.param_specs_dict,
            'tags': self.tags,
            'skin_customizations': self.skin_instance.to_dict()[
                'skin_customizations'],
            'states': {state_name: state.to_dict()
                       for (state_name, state) in self.states.iteritems()}
        })

    def to_player_dict(self):
        """Returns a copy of the exploration suitable for inclusion in the
        learner view.

        Returns:
            dict. A dict mapping some fields of Exploration instance. The
            fields inserted in the dict (as key) are:
                - init_state_name: str. The name for the initial state of the
                    exploration.
                - param_change. list(dict). List of param_change dicts that
                    represent ParamChange domain object.
                - param_specs: dict. A dict where each key-value pair
                    represents respectively, a param spec name and a dict used
                    to initialize a ParamSpec domain object.
                - skin_customizations: dict. The customization dictionary of
                    SkinInstance domain object.
                - states: dict. Keys are states names and values are dict
                    representation of State domain object.
                - title: str. The exploration title.
                - language_code: str. The language code of the exploration.
        """
        return {
            'init_state_name': self.init_state_name,
            'param_changes': self.param_change_dicts,
            'param_specs': self.param_specs_dict,
            'skin_customizations': self.skin_instance.to_dict()[
                'skin_customizations'],
            'states': {
                state_name: state.to_dict()
                for (state_name, state) in self.states.iteritems()
            },
            'title': self.title,
            'language_code': self.language_code,
        }

    def get_gadget_types(self):
        """Gets all gadget types used in this exploration.

        Returns:
            set(str). The collection of gadget types.
        """
        result = set()
        for gadget_instances in (
                self.skin_instance.panel_contents_dict.itervalues()):
            result.update([
                gadget_instance.type for gadget_instance
                in gadget_instances])
        return sorted(result)

    def get_interaction_ids(self):
        """Gets all interaction ids used in this exploration.

        Returns:
            list(str). The list of interaction ids.
        """
        return list(set([
            state.interaction.id for state in self.states.itervalues()
            if state.interaction.id is not None]))


class ExplorationSummary(object):
    """Domain object for an Oppia exploration summary."""

    def __init__(self, exploration_id, title, category, objective,
                 language_code, tags, ratings, scaled_average_rating, status,
                 community_owned, owner_ids, editor_ids,
                 viewer_ids, contributor_ids, contributors_summary, version,
                 exploration_model_created_on,
                 exploration_model_last_updated,
                 first_published_msec):
        """Initializes a ExplorationSummary domain object.

        Args:
            exploration_id: str. The exploration id.
            title: str. The exploration title.
            category: str. The exploration category.
            objective: str. The exploration objective.
            language_code: str. The code that represents the exploration
                language.
            tags: list(str). List of tags.
            ratings: dict. Dict whose keys are '1', '2', '3', '4', '5' and
                whose values are nonnegative integers representing frequency
                counts. Note that the keys need to be strings in order for this
                dict to be JSON-serializable.
            scaled_average_rating: float. The average rating.
            status: str. The status of the exploration.
            community_owned: bool. Whether the exploration is community-owned.
            owner_ids: list(str). List of the users ids who are the owners of
                this exploration.
            editor_ids: list(str). List of the users ids who have access to
                edit this exploration.
            viewer_ids: list(str). List of the users ids who have access to
                view this exploration.
            contributor_ids: list(str). List of the users ids of the user who
                have contributed to this exploration.
            contributors_summary: dict. A summary about contributors of current
                exploration. The keys are user ids and the values are the
                number of commits made by that user.
            version: int. The version of the exploration.
            exploration_model_created_on: datetime.datetime. Date and time when
                the exploration model is created.
            exploration_model_last_updated: datetime.datetime. Date and time
                when the exploration model was last updated.
            first_published_msec: int. Time in milliseconds since the Epoch,
                when the exploration was first published.
        """
        self.id = exploration_id
        self.title = title
        self.category = category
        self.objective = objective
        self.language_code = language_code
        self.tags = tags
        self.ratings = ratings
        self.scaled_average_rating = scaled_average_rating
        self.status = status
        self.community_owned = community_owned
        self.owner_ids = owner_ids
        self.editor_ids = editor_ids
        self.viewer_ids = viewer_ids
        self.contributor_ids = contributor_ids
        self.contributors_summary = contributors_summary
        self.version = version
        self.exploration_model_created_on = exploration_model_created_on
        self.exploration_model_last_updated = exploration_model_last_updated
        self.first_published_msec = first_published_msec

    def to_metadata_dict(self):
        """Given an exploration summary, this method returns a dict containing
        id, title and objective of the exploration.

        Returns:
            A metadata dict for the given exploration summary.
            The metadata dict has three keys:
                - 'id': str. The exploration ID.
                - 'title': str. The exploration title.
                - 'objective': str. The exploration objective.
        """
        return {
            'id': self.id,
            'title': self.title,
            'objective': self.objective,
        }
