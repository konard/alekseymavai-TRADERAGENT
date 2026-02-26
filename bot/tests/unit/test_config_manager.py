"""Tests for ConfigManager"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from bot.config import ConfigManager
from bot.config.schemas import AppConfig


class TestConfigManager:
    """Test ConfigManager functionality"""

    def test_load_valid_config(self, example_config_yaml: Path):
        """Test loading a valid configuration"""
        manager = ConfigManager(example_config_yaml)
        config = manager.load()

        assert isinstance(config, AppConfig)
        assert config.log_level == "INFO"
        assert len(config.bots) == 1
        assert config.bots[0].name == "test_bot"

    def test_load_nonexistent_file(self, test_config_dir: Path):
        """Test loading a non-existent file"""
        manager = ConfigManager(test_config_dir / "nonexistent.yaml")

        with pytest.raises(FileNotFoundError):
            manager.load()

    def test_load_invalid_yaml(self, test_config_dir: Path):
        """Test loading invalid YAML"""
        invalid_file = test_config_dir / "invalid.yaml"
        invalid_file.write_text("{ invalid yaml content")

        manager = ConfigManager(invalid_file)

        with pytest.raises((Exception, ValueError)):  # yaml.YAMLError or config error
            manager.load()

    def test_load_invalid_schema(self, test_config_dir: Path):
        """Test loading YAML with invalid schema"""
        invalid_file = test_config_dir / "invalid_schema.yaml"
        invalid_file.write_text("""
database_url: postgresql://test
log_level: INVALID_LEVEL
encryption_key: test
bots: []
""")

        manager = ConfigManager(invalid_file)

        with pytest.raises(ValidationError):
            manager.load()

    def test_get_config_before_load(self, example_config_yaml: Path):
        """Test getting config before loading"""
        manager = ConfigManager(example_config_yaml)

        with pytest.raises(RuntimeError):
            manager.get_config()

    def test_get_config_after_load(self, example_config_yaml: Path):
        """Test getting config after loading"""
        manager = ConfigManager(example_config_yaml)
        manager.load()

        config = manager.get_config()
        assert isinstance(config, AppConfig)

    def test_get_bot_config(self, example_config_yaml: Path):
        """Test getting specific bot configuration"""
        manager = ConfigManager(example_config_yaml)
        manager.load()

        bot_config = manager.get_bot_config("test_bot")
        assert bot_config is not None
        assert bot_config.name == "test_bot"
        assert bot_config.symbol == "BTC/USDT"

    def test_get_nonexistent_bot_config(self, example_config_yaml: Path):
        """Test getting non-existent bot configuration"""
        manager = ConfigManager(example_config_yaml)
        manager.load()

        bot_config = manager.get_bot_config("nonexistent_bot")
        assert bot_config is None

    def test_config_versioning(self, example_config_yaml: Path):
        """Test configuration version tracking"""
        manager = ConfigManager(example_config_yaml)
        manager.load()

        version1 = manager.get_config_version()
        assert isinstance(version1, str)
        assert len(version1) == 16  # SHA256 hash truncated to 16 chars

        # Reload should maintain same version
        manager.reload()
        version2 = manager.get_config_version()
        assert version1 == version2

    def test_reload_callback(self, example_config_yaml: Path):
        """Test reload callback registration"""
        manager = ConfigManager(example_config_yaml)
        manager.load()

        callback_called = []

        def test_callback(config: AppConfig):
            callback_called.append(config)

        manager.register_reload_callback(test_callback)

        # Modify config to trigger callback
        content = example_config_yaml.read_text()
        modified = content.replace("log_level: INFO", "log_level: DEBUG")
        example_config_yaml.write_text(modified)

        manager.reload()

        assert len(callback_called) == 1
        assert callback_called[0].log_level == "DEBUG"

    def test_save_config(self, example_config_yaml: Path, test_config_dir: Path):
        """Test saving configuration"""
        manager = ConfigManager(example_config_yaml)
        config = manager.load()

        # Modify config
        config.log_level = "DEBUG"

        # Save to new file
        new_file = test_config_dir / "saved_config.yaml"
        manager.save_config(config, new_file)

        # Load and verify
        manager2 = ConfigManager(new_file)
        loaded = manager2.load()
        assert loaded.log_level == "DEBUG"

    def test_create_example_config(self, test_config_dir: Path):
        """Test creating example configuration"""
        example_file = test_config_dir / "example.yaml"
        ConfigManager.create_example_config(example_file)

        assert example_file.exists()

        # Verify it can be loaded (though validation might fail due to placeholders)
        content = example_file.read_text()
        assert "database_url" in content
        assert "bots" in content
