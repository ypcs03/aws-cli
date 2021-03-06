# Copyright 2012-2013 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
from awscli.testutils import unittest
from awscli.testutils import BaseAWSCommandParamsTest
import logging

import mock
from awscli.compat import six
from botocore.vendored.requests import models
from botocore.exceptions import NoCredentialsError
from botocore.compat import OrderedDict

import awscli
from awscli.clidriver import CLIDriver
from awscli.clidriver import create_clidriver
from awscli.clidriver import CustomArgument
from awscli.clidriver import CLIOperationCaller
from awscli.clidriver import CLICommand
from awscli.clidriver import ServiceCommand
from awscli.clidriver import ServiceOperation
from awscli.customizations.commands import BasicCommand
from awscli import formatter
from botocore.hooks import HierarchicalEmitter
from botocore.provider import Provider


GET_DATA = {
    'cli': {
        'description': 'description',
        'synopsis': 'usage: foo',
        'options': {
            "debug": {
                "action": "store_true",
                "help": "Turn on debug logging"
            },
            "output": {
                "choices": [
                    "json",
                    "text",
                    "table"
                ],
                "metavar": "output_format"
            },
            "query": {
                "help": "<p>A JMESPath query to use in filtering the response data.</p>"
            },
            "profile": {
                "help": "Use a specific profile from your credential file",
                "metavar": "profile_name"
            },
            "region": {
                "choices": "{provider}/_regions",
                "metavar": "region_name"
            },
            "endpoint-url": {
                "help": "Override service's default URL with the given URL",
                "metavar": "endpoint_url"
            },
            "no-verify-ssl": {
                "action": "store_false",
                "dest": "verify_ssl",
                "help": "Override default behavior of verifying SSL certificates"
            },
            "no-paginate": {
                "action": "store_false",
                "help": "Disable automatic pagination",
                "dest": "paginate"
            },
            "page-size": {
            "type": "int",
            "help": "<p>Specifies the page size when paginating.</p>"
            },
        }
    },
    'aws/_services': {'s3':{}},
    'aws/_regions': {},
}

GET_VARIABLE = {
    'provider': 'aws',
    'output': 'json',
}


class FakeSession(object):
    def __init__(self, emitter=None):
        self.operation = None
        if emitter is None:
            emitter = HierarchicalEmitter()
        self.emitter = emitter
        self.provider = Provider(self, 'aws')
        self.profile = None
        self.stream_logger_args = None
        self.credentials = 'fakecredentials'

    def register(self, event_name, handler):
        self.emitter.register(event_name, handler)

    def emit(self, event_name, **kwargs):
        return self.emitter.emit(event_name, **kwargs)

    def emit_first_non_none_response(self, event_name, **kwargs):
        responses = self.emitter.emit(event_name, **kwargs)
        for _, response in responses:
            if response is not None:
                return response

    def get_available_services(self):
        return ['s3']

    def get_data(self, name):
        return GET_DATA[name]

    def get_config_variable(self, name):
        return GET_VARIABLE[name]

    def get_service(self, name):
        # Get service returns a service object,
        # so we'll just return a Mock object with
        # enough of the "right stuff".
        service = mock.Mock()
        operation = mock.Mock()
        operation.model.input_shape.members = OrderedDict([
            ('Bucket', mock.Mock()),
            ('Key', mock.Mock()),
        ])
        operation.model.input_shape.required_members = ['Bucket']
        operation.cli_name = 'list-objects'
        operation.name = 'ListObjects'
        operation.is_streaming.return_value = False
        operation.paginate.return_value.build_full_result.return_value = {
            'foo': 'paginate'}
        operation.call.return_value = (mock.Mock(), {'foo': 'bar'})
        self.operation = operation
        service.operations = [operation]
        service.name = 's3'
        service.cli_name = 's3'
        service.endpoint_prefix = 's3'
        service.get_operation.return_value = operation
        operation.service = service
        operation.service.session = self
        return service

    def user_agent(self):
        return 'user_agent'

    def set_stream_logger(self, *args, **kwargs):
        self.stream_logger_args = (args, kwargs)

    def get_credentials(self):
        return self.credentials


class FakeCommand(BasicCommand):
    def _run_main(self, args, parsed_globals):
        # We just return success. If this code is reached, it means that
        # all the logic in the __call__ method has sucessfully been run.
        # We subclass it here because the default implementation raises
        # an exception and we don't want that behavior.
        return 0


class FakeCommandVerify(FakeCommand):
    def _run_main(self, args, parsed_globals):
        # Verify passed arguments exist and then return success.
        # This will fail if the expected structure is missing, e.g.
        # if a string is passed in args instead of the expected
        # structure from a custom schema.
        assert args.bar[0]['Name'] == 'test'
        return 0


class TestCliDriver(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()

    def test_session_can_be_passed_in(self):
        driver = CLIDriver(session=self.session)
        self.assertEqual(driver.session, self.session)

    def test_paginate_rc(self):
        driver = CLIDriver(session=self.session)
        rc = driver.main('s3 list-objects --bucket foo'.split())
        self.assertEqual(rc, 0)

    def test_no_profile(self):
        driver = CLIDriver(session=self.session)
        driver.main('s3 list-objects --bucket foo'.split())
        self.assertEqual(driver.session.profile, None)

    def test_profile(self):
        driver = CLIDriver(session=self.session)
        driver.main('s3 list-objects --bucket foo --profile foo'.split())
        self.assertEqual(driver.session.profile, 'foo')

    def test_error_logger(self):
        driver = CLIDriver(session=self.session)
        driver.main('s3 list-objects --bucket foo --profile foo'.split())
        expected = {'log_level': logging.ERROR, 'logger_name': 'awscli'}
        self.assertEqual(driver.session.stream_logger_args[1], expected)


class TestCliDriverHooks(unittest.TestCase):
    # These tests verify the proper hooks are emitted in clidriver.
    def setUp(self):
        self.session = FakeSession()
        self.emitter = mock.Mock()
        self.emitter.emit.return_value = []
        self.stdout = six.StringIO()
        self.stderr = six.StringIO()
        self.stdout_patch = mock.patch('sys.stdout', self.stdout)
        #self.stdout_patch.start()
        self.stderr_patch = mock.patch('sys.stderr', self.stderr)
        self.stderr_patch.start()

    def tearDown(self):
        #self.stdout_patch.stop()
        self.stderr_patch.stop()

    def assert_events_fired_in_order(self, events):
        args = self.emitter.emit.call_args_list
        actual_events = [arg[0][0] for arg in args]
        self.assertEqual(actual_events, events)

    def serialize_param(self, param, value, **kwargs):
        if kwargs['cli_argument'].name == 'bucket':
            return value + '-altered!'

    def test_expected_events_are_emitted_in_order(self):
        self.emitter.emit.return_value = []
        self.session.emitter = self.emitter
        driver = CLIDriver(session=self.session)
        driver.main('s3 list-objects --bucket foo'.split())
        self.assert_events_fired_in_order([
            # Events fired while parser is being created.
            'building-command-table.main',
            'building-top-level-params',
            'top-level-args-parsed',
            'session-initialized',
            'building-command-table.s3',
            'building-argument-table.s3.list-objects',
            'before-building-argument-table-parser.s3.list-objects',
            'operation-args-parsed.s3.list-objects',
            'load-cli-arg.s3.list-objects.bucket',
            'process-cli-arg.s3.list-objects',
            'load-cli-arg.s3.list-objects.key',
            'calling-command.s3.list-objects'
        ])

    def test_create_help_command(self):
        # When we generate the HTML docs, we don't actually run
        # commands, we just call the create_help_command methods.
        # We want to make sure that in this case, the corresponding
        # building-command-table events are fired.
        # The test above will prove that is true when running a command.
        # This test proves it is true when generating the HTML docs.
        self.emitter.emit.return_value = []
        self.session.emitter = self.emitter
        driver = CLIDriver(session=self.session)
        main_hc = driver.create_help_command()
        command = main_hc.command_table['s3']
        command.create_help_command()
        self.assert_events_fired_in_order([
            # Events fired while parser is being created.
            'building-command-table.main',
            'building-top-level-params',
            'building-command-table.s3',
        ])

    def test_cli_driver_changes_args(self):
        emitter = HierarchicalEmitter()
        emitter.register('process-cli-arg.s3.list-objects', self.serialize_param)
        self.session.emitter = emitter
        driver = CLIDriver(session=self.session)
        driver.main('s3 list-objects --bucket foo'.split())
        self.assertIn(mock.call.paginate(mock.ANY, Bucket='foo-altered!'),
                      self.session.operation.method_calls)

    def test_unknown_params_raises_error(self):
        driver = CLIDriver(session=self.session)
        rc = driver.main('s3 list-objects --bucket foo --unknown-arg foo'.split())
        self.assertEqual(rc, 255)
        self.assertIn('Unknown options', self.stderr.getvalue())

    def test_unknown_command_suggests_help(self):
        driver = CLIDriver(session=self.session)
        # We're catching SystemExit here because this is raised from the bowels
        # of argparser so short of patching the ArgumentParser's exit() method,
        # we can just catch SystemExit.
        with self.assertRaises(SystemExit):
            # Note the typo in 'list-objects'
            driver.main('s3 list-objecst --bucket foo --unknown-arg foo'.split())
        # Tell the user what went wrong.
        self.assertIn("Invalid choice: 'list-objecst'", self.stderr.getvalue())
        # Offer the user a suggestion.
        self.assertIn("maybe you meant:\n\n  * list-objects", self.stderr.getvalue())


class TestSearchPath(unittest.TestCase):
    def tearDown(self):
        six.moves.reload_module(awscli)

    @mock.patch('os.pathsep', ';')
    @mock.patch('os.environ', {'AWS_DATA_PATH': 'c:\\foo;c:\\bar'})
    def test_windows_style_search_path(self):
        driver = CLIDriver()
        # Because the os.environ patching happens at import time,
        # we have to force a reimport of the module to test our changes.
        six.moves.reload_module(awscli)
        # Our two overrides should be the last two elements in the search path.
        search_path = driver.session.get_component(
            'data_loader').get_search_paths()[:-2]
        self.assertEqual(search_path, ['c:\\foo', 'c:\\bar'])


class TestAWSCommand(BaseAWSCommandParamsTest):
    # These tests will simulate running actual aws commands
    # but with the http part mocked out.
    def setUp(self):
        super(TestAWSCommand, self).setUp()
        self.stderr = six.StringIO()
        self.stderr_patch = mock.patch('sys.stderr', self.stderr)
        self.stderr_patch.start()

    def tearDown(self):
        super(TestAWSCommand, self).tearDown()
        self.stderr_patch.stop()

    def record_get_endpoint_args(self, *args, **kwargs):
        self.get_endpoint_args = (args, kwargs)
        self.real_get_endpoint(*args, **kwargs)

    def inject_new_param(self, argument_table, **kwargs):
        argument = CustomArgument('unknown-arg', {})
        argument.add_to_arg_table(argument_table)

    def inject_new_param_no_paramfile(self, argument_table, **kwargs):
        argument = CustomArgument('unknown-arg', no_paramfile=True)
        argument.add_to_arg_table(argument_table)

    def inject_command(self, command_table, session, **kwargs):
        command = FakeCommand(session)
        command.NAME = 'foo'
        command.ARG_TABLE = [
            {'name': 'bar', 'action': 'store'}
        ]
        command_table['foo'] = command

    def inject_command_schema(self, command_table, session, **kwargs):
        command = FakeCommandVerify(session)
        command.NAME = 'foo'

        # Build a schema using all the types we are interested in
        schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Name": {
                        "type": "string",
                        "required": True
                    },
                    "Count": {
                        "type": "integer"
                    }
                }
            }
        }

        command.ARG_TABLE = [
            {'name': 'bar', 'schema': schema}
        ]

        command_table['foo'] = command

    def test_aws_with_endpoint_url(self):
        with mock.patch('botocore.service.Service.get_endpoint') as endpoint:
            http_response = models.Response()
            http_response.status_code = 200
            endpoint.return_value.make_request.return_value = (
                http_response, {})
            self.assert_params_for_cmd(
                'ec2 describe-instances --endpoint-url https://foobar.com/',
                expected_rc=0)
        endpoint.assert_called_with(region_name=None,
                                    verify=None,
                                    endpoint_url='https://foobar.com/')

    def test_aws_with_region(self):
        with mock.patch('botocore.service.Service.get_endpoint') as endpoint:
            http_response = models.Response()
            http_response.status_code = 200
            endpoint.return_value.make_request.return_value = (
                http_response, {})
            self.assert_params_for_cmd(
                'ec2 describe-instances --region us-east-1',
                expected_rc=0)
        endpoint.assert_called_with(region_name='us-east-1',
                                    verify=None,
                                    endpoint_url=None)

    def test_aws_with_verify_false(self):
        with mock.patch('botocore.service.Service.get_endpoint') as endpoint:
            http_response = models.Response()
            http_response.status_code = 200
            endpoint.return_value.make_request.return_value = (
                http_response, {})
            self.assert_params_for_cmd(
                'ec2 describe-instances --region us-east-1 --no-verify-ssl',
                expected_rc=0)
        # Because we used --no-verify-ssl, get_endpoint should be
        # called with verify=False
        endpoint.assert_called_with(region_name='us-east-1',
                                    verify=False,
                                    endpoint_url=None)

    def test_aws_with_cacert_env_var(self):
        with mock.patch('botocore.endpoint.Endpoint') as endpoint:
            http_response = models.Response()
            http_response.status_code = 200
            endpoint.return_value.host = ''
            endpoint.return_value.make_request.return_value = (
                http_response, {})
            self.environ['AWS_CA_BUNDLE'] = '/path/cacert.pem'
            self.assert_params_for_cmd(
                'ec2 describe-instances --region us-east-1',
                expected_rc=0)
        call_args = endpoint.call_args
        self.assertEqual(call_args[1]['verify'], '/path/cacert.pem')

    def test_default_to_verifying_ssl(self):
        with mock.patch('botocore.endpoint.Endpoint') as endpoint:
            http_response = models.Response()
            http_response.status_code = 200
            endpoint.return_value.host = ''
            endpoint.return_value.make_request.return_value = (
                http_response, {})
            self.assert_params_for_cmd(
                'ec2 describe-instances --region us-east-1',
                expected_rc=0)
        call_args = endpoint.call_args
        self.assertEqual(call_args[1]['verify'], True)

    def test_s3_with_region_and_endpoint_url(self):
        with mock.patch('botocore.service.Service.get_endpoint') as endpoint:
            http_response = models.Response()
            http_response.status_code = 200
            endpoint.return_value.make_request.return_value = (
                http_response, {'CommonPrefixes': [], 'Contents': []})
            self.assert_params_for_cmd(
                's3 ls s3://test --region us-east-1 --endpoint-url https://foobar.com/',
                expected_rc=0)
        endpoint.assert_called_with(region_name='us-east-1',
                                    endpoint_url='https://foobar.com/',
                                    verify=None)

    def test_s3_with_no_verify_ssl(self):
        with mock.patch('botocore.service.Service.get_endpoint') as endpoint:
            http_response = models.Response()
            http_response.status_code = 200
            endpoint.return_value.make_request.return_value = (
                http_response, {'CommonPrefixes': [], 'Contents': []})
            self.assert_params_for_cmd(
                's3 ls s3://test --no-verify-ssl',
                expected_rc=0)
        endpoint.assert_called_with(region_name=None,
                                    endpoint_url=None,
                                    verify=False)

    def test_event_emission_for_top_level_params(self):
        driver = create_clidriver()
        # --unknown-foo is an known arg, so we expect a 255 rc.
        rc = driver.main('ec2 describe-instances --unknown-arg foo'.split())
        self.assertEqual(rc, 255)
        self.assertIn('Unknown options: --unknown-arg', self.stderr.getvalue())

        # The argument table is memoized in the CLIDriver object. So
        # when we call main() above, it will get created and cached
        # and the argument table won't get created again (and therefore
        # the building-top-level-params event will not get generated again).
        # So, for this test we need to create a new driver object.
        driver = create_clidriver()
        driver.session.register(
            'building-top-level-params', self.inject_new_param)
        driver.session.register(
            'top-level-args-parsed',
            lambda parsed_args, **kwargs: args_seen.append(parsed_args))

        args_seen = []

        # Now we should get an rc of 0 as the arg is expected
        # (though nothing actually does anything with the arg).
        self.patch_make_request()
        rc = driver.main('ec2 describe-instances --unknown-arg foo'.split())
        self.assertEqual(rc, 0)
        self.assertEqual(len(args_seen), 1)
        self.assertEqual(args_seen[0].unknown_arg, 'foo')

    def test_custom_arg_paramfile(self):
        with mock.patch('awscli.handlers.uri_param',
                        return_value=None) as uri_param_mock:
            driver = create_clidriver()
            driver.session.register(
                'building-argument-table', self.inject_new_param)

            self.patch_make_request()
            rc = driver.main(
                'ec2 describe-instances --unknown-arg file:///foo'.split())

            self.assertEqual(rc, 0)

            # Make sure uri_param was called
            uri_param_mock.assert_called()
            # Make sure it was called with our passed-in URI
            self.assertEqual('file:///foo',
                             uri_param_mock.call_args_list[-1][1]['value'])

    def test_custom_command_paramfile(self):
        with mock.patch('awscli.handlers.uri_param',
                        return_value=None) as uri_param_mock:
            driver = create_clidriver()
            driver.session.register(
                'building-command-table', self.inject_command)

            self.patch_make_request()
            rc = driver.main(
                'ec2 foo --bar file:///foo'.split())

            self.assertEqual(rc, 0)

            uri_param_mock.assert_called()

    @unittest.skip
    def test_custom_arg_no_paramfile(self):
        driver = create_clidriver()
        driver.session.register(
            'building-argument-table', self.inject_new_param_no_paramfile)

        self.patch_make_request()
        rc = driver.main(
            'ec2 describe-instances --unknown-arg file:///foo'.split())

        self.assertEqual(rc, 0)

    def test_custom_command_schema(self):
        driver = create_clidriver()
        driver.session.register(
            'building-command-table', self.inject_command_schema)

        self.patch_make_request()

        # Test single shorthand item
        rc = driver.main(
            'ec2 foo --bar Name=test,Count=4'.split())

        self.assertEqual(rc, 0)

        # Test shorthand list of items with optional values
        rc = driver.main(
            'ec2 foo --bar Name=test,Count=4 Name=another'.split())

        self.assertEqual(rc, 0)

        # Test missing require shorthand item
        rc = driver.main(
            'ec2 foo --bar Count=4'.split())

        self.assertEqual(rc, 255)

        # Test extra unknown shorthand item
        rc = driver.main(
            'ec2 foo --bar Name=test,Unknown='.split())

        self.assertEqual(rc, 255)

        # Test long form JSON
        rc = driver.main(
            'ec2 foo --bar {"Name":"test","Count":4}'.split())

        self.assertEqual(rc, 0)

        # Test malformed long form JSON
        rc = driver.main(
            'ec2 foo --bar {"Name":"test",Count:4}'.split())

        self.assertEqual(rc, 255)

    def test_empty_params_gracefully_handled(self):
        # Simulates the equivalent in bash: --identifies ""
        cmd = 'ses get-identity-dkim-attributes --identities'.split()
        cmd.append('')
        self.assert_params_for_cmd(cmd,expected_rc=0)

    def test_file_param_does_not_exist(self):
        driver = create_clidriver()
        rc = driver.main('ec2 describe-instances '
                         '--filters file://does/not/exist.json'.split())
        self.assertEqual(rc, 255)
        self.assertIn("Error parsing parameter '--filters': "
                      "file does not exist: does/not/exist.json",
                      self.stderr.getvalue())

    def test_aws_configure_in_error_message_no_credentials(self):
        driver = create_clidriver()
        def raise_exception(*args, **kwargs):
            raise NoCredentialsError()
        driver.session.register(
            'building-command-table',
            lambda command_table, **kwargs: \
                command_table.__setitem__('ec2', raise_exception))
        with mock.patch('sys.stderr') as f:
            driver.main('ec2 describe-instances'.split())
        self.assertEqual(
            f.write.call_args_list[0][0][0],
            'Unable to locate credentials. '
            'You can configure credentials by running "aws configure".')

    def test_override_calling_command(self):
        self.driver = create_clidriver()

        # Make a function that will return an override such that its value
        # is used over whatever is returned by the invoker which is usually
        # zero.
        def override_with_rc(**kwargs):
            return 20

        self.driver.session.register('calling-command', override_with_rc)
        rc = self.driver.main('ec2 describe-instances'.split())
        # Check that the overriden rc is as expected.
        self.assertEqual(rc, 20)

    def test_override_calling_command_error(self):
        self.driver = create_clidriver()

        # Make a function that will return an error. The handler will cause
        # an error to be returned and later raised.
        def override_with_error(**kwargs):
            return ValueError()

        self.driver.session.register('calling-command', override_with_error)
        # An exception should be thrown as a result of the handler, which
        # will result in 255 rc.
        rc = self.driver.main('ec2 describe-instances'.split())
        self.assertEqual(rc, 255)


class TestHTTPParamFileDoesNotExist(BaseAWSCommandParamsTest):

    def setUp(self):
        super(TestHTTPParamFileDoesNotExist, self).setUp()
        self.stderr = six.StringIO()
        self.stderr_patch = mock.patch('sys.stderr', self.stderr)
        self.stderr_patch.start()

    def tearDown(self):
        super(TestHTTPParamFileDoesNotExist, self).tearDown()
        self.stderr_patch.stop()

    def test_http_file_param_does_not_exist(self):
        error_msg = ("Error parsing parameter '--filters': "
                     "Unable to retrieve http://does/not/exist.json: "
                     "received non 200 status code of 404")
        with mock.patch('botocore.vendored.requests.get') as get:
            get.return_value.status_code = 404
            self.assert_params_for_cmd(
                'ec2 describe-instances --filters http://does/not/exist.json',
                expected_rc=255, stderr_contains=error_msg)


class TestCLIOperationCaller(BaseAWSCommandParamsTest):
    def setUp(self):
        super(TestCLIOperationCaller, self).setUp()
        self.session = mock.Mock()

    def test_invoke_with_no_credentials(self):
        # This is what happens you have no credentials.
        # get_credentials() return None.
        self.session.get_credentials.return_value = None
        caller = CLIOperationCaller(self.session)
        with self.assertRaises(NoCredentialsError):
            caller.invoke(None, None, None)


class TestVerifyArgument(BaseAWSCommandParamsTest):
    def setUp(self):
        super(TestVerifyArgument, self).setUp()
        self.driver.session.register('top-level-args-parsed', self.record_args)
        self.recorded_args = None

    def record_args(self, parsed_args, **kwargs):
        self.recorded_args = parsed_args

    def test_no_verify_argument(self):
        self.assert_params_for_cmd('s3api list-buckets --no-verify-ssl'.split())
        self.assertFalse(self.recorded_args.verify_ssl)

    def test_verify_argument_is_none_by_default(self):
        self.assert_params_for_cmd('s3api list-buckets'.split())
        self.assertIsNone(self.recorded_args.verify_ssl)


class TestFormatter(BaseAWSCommandParamsTest):
    def test_bad_output(self):
        with self.assertRaises(ValueError):
            formatter.get_formatter('bad-type', None)


class TestCLICommand(unittest.TestCase):
    def setUp(self):
        self.cmd = CLICommand()

    def test_name(self):
        with self.assertRaises(NotImplementedError):
            self.cmd.name
        with self.assertRaises(NotImplementedError):
            self.cmd.name = 'foo'

    def test_lineage(self):
        self.assertEqual(self.cmd.lineage, [self.cmd])

    def test_lineage_names(self):
        with self.assertRaises(NotImplementedError):
            self.cmd.lineage_names

    def test_arg_table(self):
        self.assertEqual(self.cmd.arg_table, {})


class TestServiceCommand(unittest.TestCase):
    def setUp(self):
        self.name = 'foo'
        self.session = FakeSession()
        self.cmd = ServiceCommand(self.name, self.session)

    def test_name(self):
        self.assertEqual(self.cmd.name, self.name)
        self.cmd.name = 'bar'
        self.assertEqual(self.cmd.name, 'bar')

    def test_lineage(self):
        cmd = CLICommand()
        self.assertEqual(self.cmd.lineage, [self.cmd])
        self.cmd.lineage = [cmd]
        self.assertEqual(self.cmd.lineage, [cmd])

    def test_lineage_names(self):
        self.assertEqual(self.cmd.lineage_names, ['foo'])

    def test_pass_lineage_to_child(self):
        # In order to introspect the service command's subcommands
        # we introspect the subcommand via the help command since
        # a service command's command table is not public.
        help_command = self.cmd.create_help_command()
        child_cmd = help_command.command_table['list-objects']
        self.assertEqual(child_cmd.lineage,
                         [self.cmd, child_cmd])
        self.assertEqual(child_cmd.lineage_names, ['foo', 'list-objects'])

    def test_help_event_class(self):
        # Ensures it sends the right event name to the help command
        help_command = self.cmd.create_help_command()
        self.assertEqual(help_command.event_class, 'foo')
        child_cmd = help_command.command_table['list-objects']
        # Check the ``ServiceOperation`` class help command as well
        child_help_cmd = child_cmd.create_help_command()
        self.assertEqual(child_help_cmd.event_class, 'foo.list-objects')


class TestServiceOperation(unittest.TestCase):
    def setUp(self):
        self.name = 'foo'
        self.cmd = ServiceOperation(self.name, None, None, None, None)

    def test_name(self):
        self.assertEqual(self.cmd.name, self.name)
        self.cmd.name = 'bar'
        self.assertEqual(self.cmd.name, 'bar')

    def test_lineage(self):
        cmd = CLICommand()
        self.assertEqual(self.cmd.lineage, [self.cmd])
        self.cmd.lineage = [cmd]
        self.assertEqual(self.cmd.lineage, [cmd])

    def test_lineage_names(self):
        self.assertEqual(self.cmd.lineage_names, ['foo'])


if __name__ == '__main__':
    unittest.main()
