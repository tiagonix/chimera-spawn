"""Template rendering utilities."""

import logging
from typing import Any, Dict
from jinja2 import Template, Environment, BaseLoader, TemplateError


logger = logging.getLogger(__name__)


class StringTemplateLoader(BaseLoader):
    """Template loader for string templates."""
    
    def __init__(self, template_string: str):
        self.template_string = template_string
        
    def get_source(self, environment, template):
        return self.template_string, None, lambda: True


def render_template(template_str: str, **context: Any) -> str:
    """Render a Jinja2 template string with given context."""
    try:
        # Create environment with string loader
        env = Environment(loader=StringTemplateLoader(template_str))
        
        # Get template
        template = env.get_template("")
        
        # Render with context
        return template.render(**context)
        
    except TemplateError as e:
        logger.error(f"Template rendering error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected template error: {e}")
        raise


def merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge two dictionaries."""
    result = base.copy()
    
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = value
            
    return result


def format_yaml_multiline(text: str, indent: int = 0) -> str:
    """Format multiline text for YAML output."""
    lines = text.strip().split('\n')
    if len(lines) == 1:
        return text
        
    # Use literal style for multiline
    result = "|\n"
    indent_str = " " * indent
    for line in lines:
        result += f"{indent_str}{line}\n"
        
    return result.rstrip()
