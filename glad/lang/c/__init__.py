import re
from collections import namedtuple

import itertools

from glad.config import Config, ConfigOption, RequirementConstraint
from glad.lang.generator import BaseGenerator

_ARRAY_RE = re.compile(r'\[\d*\]')

DebugArguments = namedtuple('_DebugParams', ['impl', 'function', 'callback', 'ret'])


def type_to_c(ogl_type):
    result = ''
    for text in ogl_type.element.itertext():
        if text == ogl_type.name:
            # yup * is sometimes part of the name
            result += '*' * text.count('*')
        else:
            result += text
    result = _ARRAY_RE.sub('*', result)
    return result.strip()


def params_to_c(params):
    return ', '.join(param.type.raw for param in params)


def get_debug_impl(command, command_code_name=None):
    command_code_name = command_code_name or command.proto.name

    impl = ', '.join(
        '{type} arg{i}'.format(type=type_to_c(param.type), i=i)
        for i, param in enumerate(command.params)
    )

    func = ', '.join('arg{}'.format(i) for i, _ in enumerate(command.params))
    callback = ', '.join(filter(None, [
        '"{}"'.format(command.proto.name),
        '(void*){}'.format(command_code_name),
        str(len(command.params)),
        func
    ]))

    ret = ('', '', '')
    # lower because of win API having VOID
    if not type_to_c(command.proto.ret).lower() == 'void':
        ret = (
            '{} ret;'.format(type_to_c(command.proto.ret)),
            'ret = ',
            'return ret;'
        )

    return DebugArguments(impl, func, callback, ret)


def make_ctx_func(is_mx, api_prefix):
    def ctx(name, context='context', raw=False, name_only=False):
        prefix = ''
        if is_mx:
            prefix = context + '->'
            if name.startswith('GLAD_'):
                name = name[5:]
            # glFoo -> Foo
            # GL_ARB_asd -> ARB_asd
            if not raw and name.lower().startswith(api_prefix):
                name = name[len(api_prefix):].lstrip('_')

        # 3DFX_tbuffer -> _3DFX_tbuffer
        if not name[0].isalpha():
            name = '_' + name

        if name_only:
            return name
        return prefix + name
    return ctx


def collect_alias_information(commands):
    # Thanks @derhass
    # https://github.com/derhass/glad/commit/9302dc566c695aebece901809f170297627950c9#diff-25f472d6fbc5268fe9a449252923b693

    # keep a dictionary, store the set of aliases known for each function
    # initialize it to identity, each function aliases itself
    alias = dict([command.proto.name, set([command.proto.name])] for command in commands)
    # now, add all further aliases
    for command in commands:
        if command.alias is not None:
            # aliasses is the set of all aliasses known for this function
            aliasses = alias[command.proto.name]
            aliasses.add(command.alias)
            # unify all alias sets of all aliased functions
            new_aliasses=set()
            missing_funcs=set()
            for aliased_func in aliasses:
                try:
                    new_aliasses.update(alias[aliased_func])
                except KeyError:
                    missing_funcs.add(aliased_func)
            # remove all missing functions
            new_aliasses = new_aliasses - missing_funcs
            # add the alias set to all aliased functions
            for command in new_aliasses:
                alias[command]=new_aliasses
    # clean up the alias dict: remove entries where the set contains only one element
    for command in commands:
        if len(alias[command.proto.name]) < 2:
            del alias[command.proto.name]
    return alias


# RANDOM TODOs:
# TODO: glad_get_gl_version(), glad_get_egl_version(), glad_get_*_version()
# TODO: glad_loader.h
# TODO: merge option -> https://github.com/Dav1dde/glad/issues/24


class CConfig(Config):
    DEBUG = ConfigOption(
        converter=bool,
        default=False,
        description='Enables generation of a debug build'
    )
    ALIAS = ConfigOption(
        converter=bool,
        default=False,
        description='Enables function pointer aliasing'
    )
    MX = ConfigOption(
        converter=bool,
        default=False,
        description='Enables support for multiple GL contexts'
    )
    MX_GLOBAL = ConfigOption(
        converter=bool,
        default=False,
        description='Mimic global GL functions with context switching'
    )
    HEADER_ONLY = ConfigOption(
        converter=bool,
        default=False,
        description='Generate a header only version of glad'
    )
    LOADER = ConfigOption(
        converter=bool,
        default=False,
        description='Include internal loaders for APIs'
    )

    __constraints__ = [
        RequirementConstraint(['MX_GLOBAL'], 'MX'),
        RequirementConstraint(['MX', 'DEBUG'], 'MX_GLOBAL')
    ]


class CGenerator(BaseGenerator):
    TEMPLATES = ['glad.lang.c']
    Config = CConfig

    def __init__(self, *args, **kwargs):
        BaseGenerator.__init__(self, *args, **kwargs)

        self.environment.globals.update(
            type_to_c=type_to_c,
            params_to_c=params_to_c,
            get_debug_impl=get_debug_impl,
            chain=itertools.chain,
        )

    def get_additional_template_arguments(self, spec, feature_set, options):
        return {
            'ctx': make_ctx_func(options['MX'] and feature_set.api.startswith('gl'), feature_set.api.lower()),
            'aliases': collect_alias_information(feature_set.commands)
        }

    def get_templates(self, spec, feature_set, options):
        header = 'include/glad/glad_{}.h'.format(feature_set.api)
        source = 'src/glad_{}.c'.format(feature_set.api)

        if feature_set.api == 'gl':
            header = 'include/glad/glad.h'
            source = 'src/glad.c'

        templates = list()

        if options['HEADER_ONLY']:
            templates.extend([
                ('header_only.h', header)
            ])
        else:
            templates.extend([
                ('{}.h'.format(spec.name), header),
                ('{}.c'.format(spec.name), source)
            ])

        # if header only, the loader is conditionally included by header_only.h
        if options['LOADER'] and not options['HEADER_ONLY']:
            templates.extend([
                ('loader/loader.h', 'include/glad/glad_loader.h'),
                ('loader/loader.c', 'src/glad_loader.c')
            ])

        return templates

    def modify_feature_set(self, spec, feature_set, options):
        self._fix_issue_70(feature_set)
        feature_set = self._add_extensions_for_aliasing(spec, feature_set, options)
        return feature_set

    def _add_extensions_for_aliasing(self, spec, feature_set, options):
        if not options['ALIAS']:
            return feature_set

        command_names = [command.proto.name for command in feature_set.commands]

        new_extensions = set(ext.name for ext in feature_set.extensions)
        for extension in spec.extensions[feature_set.api].values():
            if extension in feature_set.extensions:
                continue

            for command in extension.get_requirements(spec, feature_set.api, feature_set.profile).commands:
                # find all extensions which have an alias to a selected function
                if command.alias and command.alias in command_names:
                    new_extensions.add(extension.name)
                    break

                # find all extensions that have a function with the same name
                if command.proto.name in command_names:
                    new_extensions.add(extension.name)
                    break

        return spec.select(feature_set.api, feature_set.version, feature_set.profile, new_extensions)

    def _fix_issue_70(self, feature_set):
        """
        See issue #70: https://github.com/Dav1dde/glad/issues/70
        > it seems OSX already includes GLsizeiptr and a few others.
        > The same problem happens with glad.h as well.
        > The workaround appears to be to use long instead of ptrdiff_t.
        """
        for type_name in  ('GLsizeiptr', 'GLintptr', 'GLsizeiptrARB', 'GLintptrARB'):
            if type_name in feature_set.types:
                type_element = feature_set.types[feature_set.types.index(type_name)]
                type_element.raw = \
                    '#if defined(__ENVIRONMENT_MAC_OS_X_VERSION_MIN_REQUIRED__) ' + \
                    '&& (__ENVIRONMENT_MAC_OS_X_VERSION_MIN_REQUIRED__ > 1060)\n' + \
                    type_element.raw.replace('ptrdiff_t', 'long') + '\n#else\n' + type_element.raw + '\n#endif'
