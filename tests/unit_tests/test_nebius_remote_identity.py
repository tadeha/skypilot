"""Tests for attaching a service account via `nebius.remote_identity`."""
# pylint: disable=protected-access
import importlib.util
import os
import pathlib
from unittest import mock

import jinja2
import pytest

from sky import clouds
from sky import exceptions
from sky import resources as resources_lib
from sky import skypilot_config
from sky.adaptors import nebius as nebius_adaptor
from sky.backends import backend_utils
from sky.clouds import nebius
from sky.provision.nebius import utils as nebius_utils
from sky.utils import common_utils
from sky.utils import config_utils
from sky.utils import resources_utils
from sky.utils import schemas
from sky.utils import yaml_utils

_HAS_NEBIUS_SDK = importlib.util.find_spec('nebius') is not None
nebius_sdk_required = pytest.mark.skipif(not _HAS_NEBIUS_SDK,
                                         reason='Nebius SDK is not installed')

_SA = 'serviceaccount-e00abc123'
_REGION = 'eu-north1'
_INSTANCE_TYPE = 'cpu-d3_4vcpu-16gb'


@pytest.fixture(name='set_config')
def fixture_set_config(monkeypatch):

    def _set(config):
        monkeypatch.setattr(skypilot_config, '_get_loaded_config',
                            lambda: config_utils.Config(config))
        monkeypatch.setattr(skypilot_config, 'get_active_workspace',
                            lambda: 'default')

    return _set


class TestSchema:
    """Schema of nebius.remote_identity."""

    @pytest.mark.parametrize('config', [
        {
            'nebius': {
                'remote_identity': _SA
            }
        },
        {
            'nebius': {
                'remote_identity': 'LOCAL_CREDENTIALS'
            }
        },
        {
            'nebius': {
                'remote_identity': 'no_upload'
            }
        },
        {
            'nebius': {
                'remote_identity': [{
                    'train-*': _SA
                }, {
                    '*': 'LOCAL_CREDENTIALS'
                }]
            }
        },
        {
            'nebius': {
                'region_configs': {
                    _REGION: {
                        'remote_identity': _SA
                    }
                }
            }
        },
        {
            'workspaces': {
                'team-a': {
                    'nebius': {
                        'remote_identity': _SA
                    }
                }
            }
        },
    ])
    def test_accepts(self, config):
        common_utils.validate_schema(config, schemas.get_config_schema(),
                                     'err: ')

    @pytest.mark.parametrize('identity', [
        'my-service-account',
        'NONE',
        '',
        'SERVICEACCOUNT-E00ABC',
        [{
            '*': 'my-service-account'
        }],
    ])
    def test_rejects(self, identity):
        with pytest.raises(exceptions.InvalidSkyPilotConfigError):
            common_utils.validate_schema(
                {'nebius': {
                    'remote_identity': identity
                }}, schemas.get_config_schema(), 'err: ')


class TestGetServiceAccountId:
    """Resolution of the service account to attach."""

    def test_default_is_none(self, set_config):
        set_config({})
        assert nebius._get_service_account_id(_REGION, 'c', None) is None

    @pytest.mark.parametrize('identity', ['LOCAL_CREDENTIALS', 'NO_UPLOAD'])
    def test_modes_are_none(self, set_config, identity):
        set_config({'nebius': {'remote_identity': identity}})
        assert nebius._get_service_account_id(_REGION, 'c', None) is None

    def test_string(self, set_config):
        set_config({'nebius': {'remote_identity': _SA}})
        assert nebius._get_service_account_id(_REGION, 'c', None) == _SA

    def test_cluster_name_patterns(self, set_config):
        set_config({
            'nebius': {
                'remote_identity': [{
                    'train-*': _SA
                }, {
                    'serve-*': 'LOCAL_CREDENTIALS'
                }]
            }
        })
        assert nebius._get_service_account_id(_REGION, 'train-1', None) == _SA
        assert nebius._get_service_account_id(_REGION, 'serve-1', None) is None
        assert nebius._get_service_account_id(_REGION, 'other', None) is None

    def test_region_overrides_cloud_level(self, set_config):
        set_config({
            'nebius': {
                'remote_identity': 'serviceaccount-global',
                'region_configs': {
                    _REGION: {
                        'remote_identity': _SA
                    }
                },
            }
        })
        assert nebius._get_service_account_id(_REGION, 'c', None) == _SA
        assert nebius._get_service_account_id('eu-west1', 'c',
                                              None) == 'serviceaccount-global'

    def test_workspace_overrides_global(self, set_config):
        set_config({
            'nebius': {
                'remote_identity': 'serviceaccount-global'
            },
            'workspaces': {
                'default': {
                    'nebius': {
                        'remote_identity': _SA
                    }
                }
            },
        })
        assert nebius._get_service_account_id(_REGION, 'c', None) == _SA

    def test_task_override(self, set_config):
        set_config({})
        assert nebius._get_service_account_id(
            _REGION, 'c', {'nebius': {
                'remote_identity': _SA
            }}) == _SA

    def test_service_account_mode_is_rejected(self, set_config):
        set_config({'nebius': {'remote_identity': 'SERVICE_ACCOUNT'}})
        with pytest.raises(exceptions.InvalidCloudConfigs):
            nebius._get_service_account_id(_REGION, 'c', None)


def _deploy_vars(docker_image=None):
    resources = resources_lib.Resources(
        cloud=nebius.Nebius(),
        instance_type=_INSTANCE_TYPE,
        image_id=docker_image).assert_launchable()
    with mock.patch.object(nebius_utils,
                           'get_project_by_region',
                           return_value='project-id'):
        return nebius.Nebius().make_deploy_resources_variables(
            resources=resources,
            cluster_name=resources_utils.ClusterName(display_name='c',
                                                     name_on_cloud='c-1234'),
            region=clouds.Region(_REGION),
            zones=None,
            num_nodes=1)


class TestDeployVariables:
    """Deploy variables passed to the Ray YAML template."""

    def test_without_service_account(self, set_config):
        set_config({})
        deploy_vars = _deploy_vars(docker_image='docker:ubuntu')
        assert deploy_vars['service_account_id'] is None
        assert 'docker_run_options' not in deploy_vars

    def test_with_service_account(self, set_config):
        set_config({'nebius': {'remote_identity': _SA}})
        deploy_vars = _deploy_vars(docker_image='docker:ubuntu')
        assert deploy_vars['service_account_id'] == _SA
        assert deploy_vars['docker_run_options'] == [
            '-v /mnt/cloud-metadata:/mnt/cloud-metadata:ro'
        ]


def _render_template(service_account_id):
    template_path = (pathlib.Path(nebius.__file__).parents[1] / 'templates' /
                     'nebius-ray.yml.j2')
    template = jinja2.Template(template_path.read_text(encoding='utf-8'))
    rendered = template.render(
        cluster_name_on_cloud='c',
        num_nodes=1,
        region=_REGION,
        project_id='project-id',
        use_internal_ips=False,
        use_static_ip_address=False,
        security_group='sg',
        security_group_managed_by_skypilot='true',
        docker_image=None,
        ssh_private_key='~/.ssh/key',
        ssh_proxy_command=None,
        instance_type=_INSTANCE_TYPE,
        image_id='ubuntu24.04-driverless',
        disk_size=100,
        use_spot=False,
        network_tier=None,
        disk_tier='medium',
        disk_encrypted=False,
        filesystems=[],
        service_account_id=service_account_id,
        credentials={},
        initial_setup_commands=[],
    )
    return yaml_utils.safe_load(rendered)


class TestTemplate:
    """Rendering of nebius-ray.yml.j2."""

    def test_service_account_in_node_config(self):
        config = _render_template(_SA)
        node_config = config['available_node_types']['ray_head_default'][
            'node_config']
        assert node_config['service_account_id'] == _SA
        assert 'rm -f ~/.nebius/credentials.json' in config['setup_commands'][0]

    def test_no_service_account(self):
        config = _render_template(None)
        node_config = config['available_node_types']['ray_head_default'][
            'node_config']
        assert 'service_account_id' not in node_config
        assert '~/.nebius/credentials.json' not in config['setup_commands'][0]


@mock.patch.object(skypilot_config, '_global_config_context',
                   skypilot_config.ConfigContext())
@mock.patch('sky.backends.backend_utils._get_yaml_path_from_cluster_name',
            return_value='/tmp/fake/path')
@mock.patch('sky.backends.backend_utils._deterministic_cluster_yaml_hash',
            return_value='fake-hash')
@mock.patch.object(nebius_utils,
                   'get_project_by_region',
                   return_value='project-id')
@mock.patch('sky.utils.common_utils.fill_template')
@pytest.mark.parametrize('identity, uploads_credentials, expected_sa', [
    (None, True, None),
    (_SA, False, _SA),
    ('NO_UPLOAD', False, None),
])
def test_write_cluster_config_skips_credentials_with_service_account(
        mock_fill_template, mock_project, mock_hash, mock_path, tmp_path,
        identity, uploads_credentials, expected_sa):
    del mock_project, mock_hash, mock_path
    config_path = tmp_path / 'config.yaml'
    config = {} if identity is None else {
        'nebius': {
            'remote_identity': identity
        }
    }
    yaml_utils.dump_yaml(str(config_path), config)
    excluded = []

    def _credential_mounts(excluded_clouds):
        excluded.append(set(excluded_clouds))
        return {}

    with mock.patch.dict(
            os.environ,
        {skypilot_config.ENV_VAR_SKYPILOT_CONFIG: str(config_path)}), \
            mock.patch('sky.check.get_cloud_credential_file_mounts',
                       side_effect=_credential_mounts):
        skypilot_config.reload_config()
        backend_utils.write_cluster_config(
            to_provision=resources_lib.Resources(cloud=nebius.Nebius(),
                                                 instance_type=_INSTANCE_TYPE),
            num_nodes=1,
            cluster_config_template='nebius-ray.yml.j2',
            cluster_name='my-cluster',
            local_wheel_path=pathlib.Path('/tmp/fake'),
            wheel_hash='fake-hash',
            region=clouds.Region(_REGION),
            zones=None,
            dryrun=True,
            keep_launch_fields_in_existing_config=True)

    assert (clouds.Nebius() not in excluded[0]) == uploads_credentials
    variables = mock_fill_template.call_args[0][1]
    assert variables['service_account_id'] == expected_sa


@nebius_sdk_required
@pytest.mark.parametrize('service_account_id', [None, _SA])
def test_launch_sets_instance_service_account(service_account_id):
    compute = nebius_adaptor.compute()
    service = mock.MagicMock()
    instance = mock.MagicMock()
    instance.metadata.id = 'instance-id'
    instance.status.state.name = 'STARTING'

    with mock.patch.object(nebius_utils,
                           'get_project_by_region',
                           return_value='project-id'), \
            mock.patch.object(nebius_utils,
                              'get_subnet_id',
                              return_value='subnet-id'), \
            mock.patch.object(nebius_adaptor, 'sdk'), \
            mock.patch.object(compute,
                              'InstanceServiceClient',
                              return_value=service), \
            mock.patch.object(nebius_adaptor,
                              'sync_call',
                              side_effect=[None, instance]):
        nebius_utils.launch(
            cluster_name_on_cloud='cluster',
            node_type='head',
            platform='cpu-d3',
            preset='4vcpu-16gb',
            region=_REGION,
            image_id_or_family='ubuntu24.04-driverless',
            disk_size=100,
            user_data='',
            associate_public_ip_address=True,
            filesystems=[],
            disk_tier=resources_utils.DiskTier.MEDIUM,
            service_account_id=service_account_id,
        )

    request = service.create.call_args.args[0]
    assert request.spec.service_account_id == (service_account_id or '')


class TestAdaptorVmServiceAccountToken:
    """SDK auth with the token of an attached service account."""

    @pytest.fixture(name='sdk_factory')
    def fixture_sdk_factory(self, monkeypatch, tmp_path):
        nebius_adaptor.clear_sdk_cache()
        monkeypatch.setattr(nebius_adaptor, 'api_domain', lambda: None)
        monkeypatch.setattr(nebius_adaptor.skypilot_config,
                            'get_active_workspace', lambda: 'default')
        monkeypatch.setattr(nebius_adaptor.skypilot_config,
                            'get_workspace_cloud', lambda cloud: {})
        monkeypatch.setattr(nebius_adaptor, 'get_iam_token', lambda: None)
        monkeypatch.setattr(nebius_adaptor, '_get_default_credentials_path',
                            lambda: str(tmp_path / 'credentials.json'))
        monkeypatch.setattr(nebius_adaptor,
                            '_close_sdk',
                            lambda _: None,
                            raising=False)
        token_path = tmp_path / 'token'
        monkeypatch.setattr(nebius_adaptor, 'VM_SERVICE_ACCOUNT_TOKEN_PATH',
                            str(token_path))
        factory = mock.MagicMock()
        monkeypatch.setattr(nebius_adaptor, 'nebius', factory)
        yield factory.sdk.SDK, tmp_path
        nebius_adaptor.clear_sdk_cache()

    @nebius_sdk_required
    def test_uses_vm_token_without_credentials_file(self, sdk_factory):
        # pylint: disable=import-outside-toplevel
        from nebius.aio.token import file as token_file
        factory, tmp_path = sdk_factory
        (tmp_path / 'token').write_text('vm-token')

        assert nebius_adaptor.is_token_or_cred_file_exist()
        nebius_adaptor.sdk()
        nebius_adaptor.sdk()

        factory.assert_called_once()
        bearer = factory.call_args.kwargs['credentials']
        assert isinstance(bearer, token_file.Bearer)

    def test_credentials_file_takes_precedence(self, sdk_factory):
        factory, tmp_path = sdk_factory
        (tmp_path / 'token').write_text('vm-token')
        (tmp_path / 'credentials.json').write_text('{}')

        nebius_adaptor.sdk()

        assert factory.call_args.kwargs['credentials_file_name'] == str(
            tmp_path / 'credentials.json')

    def test_no_credentials(self, sdk_factory):
        del sdk_factory
        assert not nebius_adaptor.is_token_or_cred_file_exist()
