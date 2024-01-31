![conflator logo](docs/conflator.png)

# Conflator

Conflator is a configuration-handling library for Python. It is designed to simplify the handling of configuration from multiple sources, such as environment variables, command line arguments, and configuration files. As an application or library developer, you specify your configuration schema with a Pydantic model, and conflator will handle the rest.

## Installation

Conflator is available on PyPI:

```bash
pip install conflator
```

Or using poetry:

```bash
poetry add conflator
```

## Usage

Coming soon...


## Limitations

CLIArgs won't be created for submodels which aren't loaded at first initialization.