import os
import yaml
from typing import Any, Dict, Optional

# Global translations cache
_translations = {}

def load_yaml_file(locale: str) -> Optional[Dict]:
    """Load YAML file for the given locale."""
    try:
        file_path = os.path.join(os.path.dirname(__file__), 'locales', locale, 'messages.yml')
        with open(file_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Error loading YAML file for locale {locale}: {str(e)}")
        return None

def setup_i18n():
    """Setup internationalization for the bot."""
    global _translations
    
    # Load translations into cache
    for locale in ['fa', 'en']:
        data = load_yaml_file(locale)
        if data:
            _translations[locale] = data

def get_nested_value(data: Dict, key: str) -> Any:
    """Get a nested value from a dictionary using dot notation."""
    try:
        parts = key.split('.')
        current = data
        
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                index = int(part)
                current = current[index] if 0 <= index < len(current) else None
            else:
                return None
                
            if current is None:
                return None
                
        return current
    except Exception as e:
        print(f"Error getting nested value for key {key}: {str(e)}")
        return None

def t(key: str, locale: str = 'fa', **kwargs) -> str:
    """Translate a key to the specified locale."""
    try:
        # Get translations for the requested locale
        translations = _translations.get(locale, {})
        if not translations:
            # Try to load translations if not in cache
            translations = load_yaml_file(locale)
            if translations:
                _translations[locale] = translations
        
        # Get the translation
        value = get_nested_value(translations, key)
        
        # If no translation found and locale is not Persian, try Persian
        if value is None and locale != 'fa':
            return t(key, 'fa', **kwargs)
        
        # If value is a list, return it as is
        if isinstance(value, list):
            return value
        
        # If we have a translation, format it with the provided kwargs
        if value is not None:
            result = str(value)
            for k, v in kwargs.items():
                placeholder = f"%{{{k}}}"
                result = result.replace(placeholder, str(v))
            return result
        
        # If no translation found, return the key
        return key
        
    except Exception as e:
        print(f"Translation error for key {key}: {str(e)}")
        return key

# Available languages
AVAILABLE_LANGUAGES = {
    'fa': '🇮🇷 فارسی',
    'en': '🇬🇧 English'
}

def get_language_keyboard():
    """Get the keyboard markup for language selection."""
    return {
        "inline_keyboard": [
            [
                {"text": name, "callback_data": f"lang:{code}"}
                for code, name in AVAILABLE_LANGUAGES.items()
            ]
        ]
    }

def get_language_name(code: str) -> str:
    """Get the display name of a language by its code."""
    return AVAILABLE_LANGUAGES.get(code, code) 