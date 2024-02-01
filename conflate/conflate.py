import argparse
from typing import Dict, Any, Union, Tuple
from enum import Enum
from pydantic import BaseModel, ValidationError, model_validator
from pydantic_core import PydanticUndefined
from pathlib import Path
from rich_argparse import RawTextRichHelpFormatter
import json
import os
from typing import get_origin, get_args, Type

from rich import print as rprint
from rich.tree import Tree as rtree



class CLIArg():
    def __init__(self, *args):
        self.args = args
        self.description = None
        self.argparse_key = None


class EnvVar:
    def __init__(self, name: str = None):
        # print(f"ENV VAR {name} INIT")
        self.name = name


class ConfigModel(BaseModel, revalidate_instances='always', validate_assignment=True, validate_default=True):



    @model_validator(mode='wrap')
    @classmethod
    def wrap_root(cls, unvalidated, handler, info):

        
        # Skip if not validating via Conflator
        if type(info.context) != ParseContext:
            return handler(unvalidated)
        
        # Unvalidated can be a dict or a already-intiialised Model
        def get(key, default=None):
            if isinstance(unvalidated, dict):
                return unvalidated.get(key, default)
            else:
                return getattr(unvalidated, key, default)
        def set(key, value):
            if isinstance(unvalidated, dict):
                unvalidated[key] = value
            else:
                setattr(unvalidated, key, value)

        # Retrieve set environment variables
        for k,v in cls.model_fields.items():
            env_vars = [m for m in v.metadata if type(m) == EnvVar]
            for ev in env_vars:
                set_env = os.getenv(f"{info.context.app_name.upper()}_{(ev.name or k).upper()}", None)
                if set_env is not None:
                    set(k, set_env)

        # Retrieve set CLI args
        for k,v in cls.model_fields.items():
            cli_args = [m for m in v.metadata if type(m) == CLIArg]
            for ca in cli_args:
                set_arg = getattr(info.context.cli_args, ca.argparse_key, None)
                if set_arg is not None:
                    set(k, set_arg)

        return handler(unvalidated)


class ParseContext():
    def __init__(self):
        self.app_name = None
        self.cli_args = {}

class Conflater():

    def __init__(
            self,
            app_name,
            model: type[BaseModel],
            cli=True,
            argparser : argparse.ArgumentParser = None,
            **overrides: Dict[str, Any]
    ):
        self.app_name = app_name
        self.model = model
        self.cli = cli
        self.parser = argparser
        self.overrides = overrides

        self.config_files = [
            Path()/"etc"/self.app_name/"config.json",
            Path.home()/f".{self.app_name}apirc"
        ]

    @staticmethod
    def find_models(t: Type[BaseModel], seen=None) -> set[Type[BaseModel]]:

        if seen is None:
            seen = set()

        if get_origin(t):
            for a in get_args(t):
                if isinstance(a, ConfigModel) and a not in seen:
                    Conflater.find_models(a, seen)
        else:
            if issubclass(t, ConfigModel) and t not in seen:
                seen.add(t)
                for k,v in t.__annotations__.items():
                    Conflater.find_models(v, seen)

        return seen

    @staticmethod
    def get_cli_args(model: Type[BaseModel], args: set[CLIArg] = None):
        if args is None:
            args = set()
        model_title = model.model_config.get("title") or model.__name__
        for k,v in model.model_fields.items():
            cli_args = [m for m in v.metadata if type(m) == CLIArg]
            for ca in cli_args:
                args.add(ca)
        return args


    
    def load(self) -> BaseModel:
        

        referenced_models = Conflater.find_models(self.model)
        # rprint(referenced_config_models)

        if self.cli:

            # Find all CLI args and then parse them
            cli_args = set()
            for m in referenced_models:
                cli_args |= Conflater.get_cli_args(m, cli_args)

            # Could do a pre-validation here to see if which CLI args actually *need* to be set
            # and to give more information to the user about what is already set by config

            if self.parser is None:
                self.parser = argparse.ArgumentParser(
                    description=f"All arguments can be overriden by environment variables {self.app_name.upper()}_* "
                               +f"or set in config files:\n"
                               +'\n'.join([f" - {cf}" for cf in self.config_files]),
                    formatter_class=RawTextRichHelpFormatter
                )

            for ca in cli_args:
                action = self.parser.add_argument(*ca.args, help=ca.description)
                ca.argparse_key = action.dest

            self.parser.add_argument("--set", action='append', help="Override config with dot-separated path and value, e.g. --set foo.bar.baz=1", default=[])
            self.parser.add_argument("-f", "--config", action='append', help="Override config with additional configuration files, e.g. --config ./config.yaml", default=[])

            args = self.parser.parse_args()
        
        else:
            args = {}

        # Initialise config dictionary empty
        self.loaded_config = {}

        # Then merge all config files
        config_files = self.config_files + [Path(f) for f in args.config]
        for cf in config_files:
            self._merge(Conflater._from_file(cf))

        # Then merge all --set arguments from CLI
        for setting in args.set:
            path, value = setting.split("=")
            rprint(f"SETTING {path} = {value}")
            self._merge(Conflater._dot_path_to_nested_dict(path, value))

        # Finally merge with kwargs passed to the constructor
        self.loaded_config.update(self.overrides)

        rprint(f"LOADED CONFIG {self.loaded_config}")

        # Finally, validate the model, invoking the custom validator to inject CLI args and environment variables
        parse_context = ParseContext()
        parse_context.cli_args = args
        parse_context.app_name = self.app_name

        try:
            result = self.model.model_validate(self.loaded_config, context=parse_context)
        except ValidationError as e:
            output = rtree(f"[red]Configuration errors: {e.error_count()}[/red]")
            for err in e.errors():
                output.add(f"[red]{err['msg'].upper()}:[/red][cyan] {self._loc_to_dot_sep(err['loc'])}[/cyan]")
            rprint(output)
            raise SystemExit(e.error_count())

        return result
    
    @staticmethod
    def _dot_path_to_nested_dict(path: str, value: Any, convert_hyphens_to_underscores: bool = True) -> Dict[str, Any]:
        """
        Convert a dot-separated path into a nested dictionary with the specified value.

        :param path: A string representing the dot-separated path.
        :param value: The value to set at the innermost level of the nested dictionary.
        :return: A nested dictionary representing the path.
        """
        if convert_hyphens_to_underscores:
            path = path.replace('-', '_')
        parts = path.split('.')
        nested_dict = {}
        current_level = nested_dict
        for part in parts[:-1]:
            current_level[part] = current_level.get(part, {})
            current_level = current_level[part]
        current_level[parts[-1]] = value
        return nested_dict
    
    def _merge(self, b, path=None):

        a = self.loaded_config
        # b = copy.deepcopy(b)

        if path is None:
            path = []

        if not isinstance(b, dict):
            if isinstance(a, list) and isinstance(b, list):
                return a + b
            self.loaded_config = b

        for key in b:
            if key in a:
                if b[key] is None:
                    del a[key]
                elif isinstance(a[key], dict) and isinstance(b[key], dict):
                    a[key] = self._merge(a[key], b[key], path + [str(key)])
                elif isinstance(a[key], list) and isinstance(b[key], list):
                    a[key] = a[key] + b[key]
                elif a[key] == b[key]:
                    pass
                else:
                    a[key] = b[key]
            else:
                if b[key] is not None:
                    a[key] = b[key]
        
    
    def _from_file(path: Path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except:
            return {}


    def _update_from_env(self) -> Dict:

        # Try to read from environment variables (case insensitive)
        env_vars = {k.upper(): v for k, v in os.environ.items()}
        for k,v in self.model.model_fields.items():
            key = f"{self.app_name.upper()}_{(v.alias or k).upper()}"
            if key in env_vars:
                self.loaded_config[v.alias] = env_vars[key]
        
    def _update_from_cli_args(self):

        if not self.parser:
            self.parser = argparse.ArgumentParser(
                    description=f"All arguments can be overriden with {self.app_name.upper()}_ARG."
                               +f"They can also be set in JSON files: /etc/{self.app_name}/config.json and ~/.{self.app_name}apirc"
                               +[f" - {cf}" for cf in self.config_files],
                    formatter_class=RawTextRichHelpFormatter
                )
        # Add arguments based on Pydantic model fields
        for field_name, field in self.model.model_fields.items():

            if field_name in self.loaded_config:
                default = self.loaded_config[field_name]
                help = f"{field.description or ''} (found: {self.loaded_config[field_name]})"
            else:
                default = field.default
                help = field.description
            
            self.parser.add_argument(
                f"--{field_name}",
                type=str,
                required=field.is_required and field.default is PydanticUndefined and field_name not in self.loaded_config,
                default=default,
                help=help
            )

        args = self.parser.parse_args()
        self.loaded_config.update(args.__dict__)
    
    @staticmethod
    def _loc_to_dot_sep(loc: Tuple[Union[str, int], ...]) -> str:
        path = ''
        for i, x in enumerate(loc):
            if isinstance(x, str):
                if i > 0:
                    path += ' > '
                path += x
            elif isinstance(x, int):
                path += f'[{x}]'
            else:
                raise TypeError('Unexpected type')
        return path