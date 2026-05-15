"""
Utility functions for accessing and managing Configuration objects.

Usage examples:
    from core.config_utils import get_config, set_config, get_config_value
    
    # Get entire configuration
    config = get_config('payment_settings')
    
    # Get a specific value with default
    gateway = get_config_value('payment_settings', 'gateway', 'razorpay')
    
    # Set configuration
    set_config('payment_settings', {
        'gateway': 'razorpay',
        'api_key': 'xxx',
        'timeout': 30
    })
"""

from .models import Configuration
from typing import Any, Optional, Dict


def get_config(name: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve a configuration by name.
    
    Args:
        name: Configuration name/identifier
        
    Returns:
        Configuration data dict or None if not found
    """
    try:
        config = Configuration.objects.get(name=name)
        return config.data
    except Configuration.DoesNotExist:
        return None


def set_config(name: str, data: Dict[str, Any]) -> Configuration:
    """
    Create or update a configuration.
    
    Args:
        name: Configuration name/identifier
        data: JSON-serializable configuration data
        
    Returns:
        Configuration instance
    """
    config, created = Configuration.objects.update_or_create(
        name=name,
        defaults={'data': data}
    )
    return config


def get_config_value(name: str, key: str, default: Any = None) -> Any:
    """
    Get a specific value from a configuration.
    
    Args:
        name: Configuration name/identifier
        key: Key in the configuration data dict
        default: Default value if key not found
        
    Returns:
        Configuration value or default
    """
    config_data = get_config(name)
    if config_data is None:
        return default
    return config_data.get(key, default)


def update_config_value(name: str, key: str, value: Any) -> Configuration:
    """
    Update a specific value in a configuration.
    
    Args:
        name: Configuration name/identifier
        key: Key in the configuration data dict
        value: New value
        
    Returns:
        Updated Configuration instance
    """
    config_data = get_config(name) or {}
    config_data[key] = value
    return set_config(name, config_data)
