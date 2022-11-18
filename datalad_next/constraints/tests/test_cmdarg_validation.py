import pytest
from typing import Dict

from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import patch

from datalad.interface.base import (
    Interface,
)
from datalad.interface.utils import eval_results
from datalad.support.param import Parameter
from datalad.utils import on_windows
from datalad_next.constraints.base import AltConstraints
from datalad_next.constraints import (
    EnsureGeneratorFromFileLike,
    EnsureJSON,
    EnsureListOf,
    EnsureMapping,
    EnsurePath,
    EnsureURL,
)


class CmdWithValidation(Interface):
    # this is of little relevance, no validation configured here
    _params_ = dict(spec=Parameter(args=('spec',), nargs='+'))

    url_constraint = EnsureURL(required=['scheme'])
    url2path_constraint = EnsureMapping(
        key=url_constraint, value=EnsurePath(),
        delimiter='\t'
    )
    spec_item_constraint = url2path_constraint | url_constraint \
        | (EnsureJSON() & url2path_constraint)

    # this is the key bit: a mapping of parameter names to validators
    _validators_ = dict(
        # Must not OR: https://github.com/datalad/datalad/issues/7164
        #spec=spec_item_constraint | EnsureListOf(spec_item_constraint)# \
        spec=AltConstraints(
            EnsureListOf(spec_item_constraint),
            EnsureGeneratorFromFileLike(spec_item_constraint),
            spec_item_constraint,
        ),
    )

    # validation helper, need not be a classmethod
    @classmethod
    def validate_args(cls: Interface, kwargs: Dict, at_default: set) -> Dict:
        validated = {}
        for argname, arg in kwargs.items():
            if argname in at_default:
                # do not validate any parameter where the value matches the
                # default declared in the signature. Often these are just
                # 'do-nothing' settings or have special meaning that need
                # not be communicated to a user. Not validating them has
                # two consequences:
                # - the condition can simply be referred to as "default
                #   behavior" regardless of complexity
                # - a command implementation must always be able to handle
                #   its own defaults directly, and cannot delegate a
                #   default value handling to a constraint
                #
                # we must nevertheless pass any such default value through
                # to make/keep them accessible to the general result handling
                # code
                validated[argname] = arg
                continue
            validator = cls._validators_.get(argname, lambda x: x)
            # TODO option to validate all args despite failure
            try:
                validated[argname] = validator(arg)
            except Exception as e:
                raise ValueError(
                    f'Validation of parameter {argname!r} failed') from e
        return validated

    # command implementation that only validated and returns the outcome
    @staticmethod
    @eval_results
    def __call__(spec):
        yield dict(
            action='cmd_with_validation',
            # list() consumes any potential generator
            spec=list(spec),
            status='ok',
        )


def test_cmd_with_validation():
    target_urls = ['http://example.com', 'file:///dev/null']
    target_url_path_maps = [
        {'http://example.com': Path('some/dir/file')},
        {'file:///dev/null': Path('/dev/null')},
    ]
    json_lines = '{"http://example.com":"some/dir/file"}\n' \
                 '{"file:///dev/null":"/dev/null"}'

    for input, target in (
        # perfect input
        (target_urls, target_urls),
        (target_url_path_maps, target_url_path_maps),
        # actual invput conversion
        ([{'http://example.com': 'some/dir/file'},
          {'file:///dev/null': '/dev/null'}],
         target_url_path_maps),
        # custom mapping syntax
        (['http://example.com\tsome/dir/file',
          'file:///dev/null\t/dev/null'],
         target_url_path_maps),
        # JSON lines
        (['{"http://example.com":"some/dir/file"}',
          '{"file:///dev/null":"/dev/null"}'],
         target_url_path_maps),
        # from file with JSON lines
        (StringIO(json_lines), target_url_path_maps),
    ):
        res = CmdWithValidation.__call__(
            input,
            return_type='item-or-list', result_renderer='disabled',
        )
        assert 'spec' in res
        assert res['spec'] == target

    # read from file
    if not on_windows:
        # on windows the write-rewind-test logic is not possible
        # (PermissionError) -- too lazy to implement a workaround
        with NamedTemporaryFile('w+') as f:
            f.write(json_lines)
            f.seek(0)
            res = CmdWithValidation.__call__(
                f.name,
                return_type='item-or-list', result_renderer='disabled',
            )
            assert res['spec'] == target_url_path_maps

    with patch("sys.stdin", StringIO(json_lines)):
        res = CmdWithValidation.__call__(
            '-',
            return_type='item-or-list', result_renderer='disabled',
        )
        assert res['spec'] == target_url_path_maps

    # and now something that fails
    # TODO error reporting should be standardized (likely) on an explicit
    # and dedicated exception type
    # https://github.com/datalad/datalad/issues/7167
    with pytest.raises(ValueError):
        CmdWithValidation.__call__(
            'unsupported',
            return_type='item-or-list', result_renderer='disabled',
        )