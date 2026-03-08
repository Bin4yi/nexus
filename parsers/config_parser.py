"""
parsers/config_parser.py
Configuration file parser — scans WSO2 ``deployment.toml``,
``repository/conf/*.xml``, and ``application.yml`` files.

Creates ``(Configuration)`` nodes in Neo4j and links them to
Java configuration manager classes via ``[:READS_CONFIG]`` edges.
"""
from __future__ import annotations
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Regex patterns for configuration key extraction ───────────────────────────

# deployment.toml sections: [section.subsection]
_TOML_SECTION_RE = re.compile(r"^\[([^\]]+)\]", re.MULTILINE)
# deployment.toml key=value pairs
_TOML_KV_RE = re.compile(r"^(\w[\w.]*)\s*=", re.MULTILINE)

# XML element names (simple extraction, not full parse)
_XML_ELEMENT_RE = re.compile(r"<(\w[\w.-]*)[>\s/]")
# XML property placeholders: ${property.name}
_XML_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")

# YAML top-level keys (indentation=0)
_YAML_KEY_RE = re.compile(r"^(\w[\w.-]*):", re.MULTILINE)

# Java config reader patterns — classes that read config
_CONFIG_READER_PATTERNS = re.compile(
    r"(IdentityUtil\.getProperty|CarbonUtils\.getServerConfiguration"
    r"|ServerConfiguration\.getInstance|getConfiguration"
    r"|@Value\s*\(\s*[\"']\$\{|Environment\.getProperty"
    r"|ConfigurationContextService|deployment\.toml"
    r"|IdentityConfigParser|OAuthServerConfiguration"
    r"|@ConfigurationProperties"
    r"|FileBasedConfigurationBuilder"
    r"|PrivilegedCarbonContext)",
    re.IGNORECASE,
)

# Extract the config key being read
_CONFIG_KEY_EXTRACT_RE = re.compile(
    r'(?:getProperty|getValue|getFirstProperty|getConfiguration)\s*\(\s*["\']([^"\']+)["\']',
)


@dataclass
class ConfigurationInfo:
    """Represents a discovered configuration entry."""
    config_key: str          # e.g. "oauth.token.persistence" or "[server]"
    config_type: str         # "toml" | "xml" | "yaml"
    source_file: str         # path to the config file
    repo_name: str


@dataclass
class ConfigReadEdge:
    """A Java class (by geid) → config key edge."""
    component_geid: str
    component_fqn: str
    config_key: str


class ConfigurationParser:
    """
    Scans repositories for configuration files (deployment.toml, XML configs,
    application.yml) and extracts configuration keys/sections.

    Also detects which Java classes read configuration values and links them.
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def scan_config_files(self, repo_path: Path, repo_name: str) -> list[ConfigurationInfo]:
        """
        Discover configuration files and extract their keys/sections.

        Scans:
        - deployment.toml (WSO2 Carbon 5+ primary config)
        - repository/conf/*.xml (WSO2 Carbon XML configs)
        - **/application.yml, application.yaml (Spring Boot)
        - **/identity.xml, identity-event.properties

        Returns:
            List of ConfigurationInfo entries.
        """
        configs: list[ConfigurationInfo] = []
        seen_keys: set[str] = set()

        # 1. deployment.toml
        for toml_file in self._find_files(repo_path, "deployment.toml"):
            configs.extend(self._parse_toml(toml_file, repo_name, seen_keys))

        # 2. repository/conf/*.xml and other XML config locations
        for xml_dir_pattern in [
            "repository/conf",
            "conf",
            "src/main/resources",
            "src/main/resources/conf",
        ]:
            xml_dir = repo_path / xml_dir_pattern
            if xml_dir.exists():
                for xml_file in xml_dir.glob("*.xml"):
                    configs.extend(self._parse_xml_config(xml_file, repo_name, seen_keys))

        # Also look for identity.xml, identity-event.properties anywhere
        for config_name in ["identity.xml", "identity-event.properties"]:
            for cf in repo_path.rglob(config_name):
                if "target" not in str(cf) and "test" not in str(cf).lower():
                    configs.extend(self._parse_xml_config(cf, repo_name, seen_keys))

        # 4. *.properties files (WSO2 config files)
        for props_pattern in ["src/main/resources", "repository/conf", "conf"]:
            props_dir = repo_path / props_pattern
            if props_dir.exists():
                for props_file in props_dir.glob("*.properties"):
                    configs.extend(self._scan_properties_file(props_file, repo_name, seen_keys))

        # 3. application.yml / application.yaml
        for yml_file in self._find_files(repo_path, "application.yml"):
            configs.extend(self._parse_yaml_config(yml_file, repo_name, seen_keys))
        for yml_file in self._find_files(repo_path, "application.yaml"):
            configs.extend(self._parse_yaml_config(yml_file, repo_name, seen_keys))

        if configs:
            logger.info(
                "Found %d configuration entries in %s", len(configs), repo_name,
            )
        return configs

    def detect_config_readers(
        self,
        java_files: list[Path],
        components: list,  # list[Component]
        known_config_keys: set[str],
    ) -> list[ConfigReadEdge]:
        """
        Scan Java source files for classes that read configuration values.

        Returns:
            List of ConfigReadEdge linking component → config_key.
        """
        edges: list[ConfigReadEdge] = []
        fp_to_comp = {}
        for comp in components:
            if comp.file_path:
                fp_to_comp[str(Path(comp.file_path).resolve())] = comp

        for jf in java_files:
            try:
                source = jf.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if not _CONFIG_READER_PATTERNS.search(source):
                continue

            comp = fp_to_comp.get(str(jf.resolve()))
            if not comp:
                continue

            # Extract specific config keys being read
            referenced_keys: set[str] = set()
            for m in _CONFIG_KEY_EXTRACT_RE.finditer(source):
                key = m.group(1).strip()
                if key:
                    referenced_keys.add(key)

            # Also check for @Value("${key}") Spring pattern
            for m in re.finditer(r'@Value\s*\(\s*["\']?\$\{([^}]+)\}', source):
                key = m.group(1).strip()
                if key:
                    referenced_keys.add(key)

            # If no specific keys found but class is a config reader,
            # link to a generic config node for the class
            if not referenced_keys and _CONFIG_READER_PATTERNS.search(source):
                # Try to match against known config keys by substring
                for key in known_config_keys:
                    key_parts = key.split(".")
                    for part in key_parts:
                        if len(part) > 3 and part.lower() in source.lower():
                            referenced_keys.add(key)
                            break

            for key in referenced_keys:
                edges.append(ConfigReadEdge(
                    component_geid=comp.geid,
                    component_fqn=comp.fqn,
                    config_key=key,
                ))

        if edges:
            logger.info("Detected %d READS_CONFIG edges", len(edges))
        return edges

    # ── Private ───────────────────────────────────────────────────────────────

    def _find_files(self, repo_path: Path, filename: str) -> list[Path]:
        """Find all instances of *filename* under *repo_path*, skipping build dirs."""
        results = []
        for p in repo_path.rglob(filename):
            s = str(p)
            if "target" not in s and "node_modules" not in s and ".git" not in s:
                results.append(p)
        return results

    def _parse_toml(
        self, toml_file: Path, repo_name: str, seen: set[str],
    ) -> list[ConfigurationInfo]:
        """Parse a deployment.toml file using tomllib (stdlib, Python 3.11+) for
        accurate hierarchy. Falls back to regex-based parsing for older Python."""
        configs: list[ConfigurationInfo] = []

        # Use tomllib (stdlib 3.11+) for accurate nested table handling
        if sys.version_info >= (3, 11):
            try:
                import tomllib
                with open(toml_file, "rb") as f:
                    data = tomllib.load(f)
                self._flatten_toml_dict(data, "", toml_file, repo_name, seen, configs)
                return configs
            except Exception as e:
                logger.debug("tomllib parse failed for %s (%s), falling back to regex", toml_file, e)

        # Regex fallback for Python < 3.11 or malformed TOML
        try:
            content = toml_file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("Cannot read %s: %s", toml_file, e)
            return configs

        current_section = ""
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            section_m = _TOML_SECTION_RE.match(line)
            if section_m:
                current_section = section_m.group(1)
                key = f"[{current_section}]"
                if key not in seen:
                    seen.add(key)
                    configs.append(ConfigurationInfo(
                        config_key=key, config_type="toml",
                        source_file=str(toml_file), repo_name=repo_name,
                    ))
                continue
            kv_m = _TOML_KV_RE.match(line)
            if kv_m and current_section:
                full_key = f"{current_section}.{kv_m.group(1)}"
                if full_key not in seen:
                    seen.add(full_key)
                    configs.append(ConfigurationInfo(
                        config_key=full_key, config_type="toml",
                        source_file=str(toml_file), repo_name=repo_name,
                    ))
        return configs

    def _flatten_toml_dict(
        self, data: dict, prefix: str, source_file: Path,
        repo_name: str, seen: set[str], configs: list[ConfigurationInfo],
    ) -> None:
        """Recursively flatten a tomllib dict into dotted config keys."""
        for k, v in data.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                # Nested table — record the section and recurse
                section_key = f"[{full_key}]"
                if section_key not in seen:
                    seen.add(section_key)
                    configs.append(ConfigurationInfo(
                        config_key=section_key, config_type="toml",
                        source_file=str(source_file), repo_name=repo_name,
                    ))
                self._flatten_toml_dict(v, full_key, source_file, repo_name, seen, configs)
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                # Array of tables [[section]]
                for i, item in enumerate(v):
                    self._flatten_toml_dict(item, full_key, source_file, repo_name, seen, configs)
            else:
                # Leaf value — record as dotted key
                if full_key not in seen:
                    seen.add(full_key)
                    configs.append(ConfigurationInfo(
                        config_key=full_key, config_type="toml",
                        source_file=str(source_file), repo_name=repo_name,
                    ))

    def _scan_properties_file(
        self, props_file: Path, repo_name: str, seen: set[str],
    ) -> list[ConfigurationInfo]:
        """Parse Java *.properties files for key=value entries."""
        configs: list[ConfigurationInfo] = []
        try:
            content = props_file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("Cannot read %s: %s", props_file, e)
            return configs

        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            # key=value or key: value
            m = re.match(r'^([\w.\-/]+)\s*[=:]', line)
            if m:
                key = m.group(1).strip()
                if key not in seen:
                    seen.add(key)
                    configs.append(ConfigurationInfo(
                        config_key=key,
                        config_type="properties",
                        source_file=str(props_file),
                        repo_name=repo_name,
                    ))
        return configs

    def _parse_xml_config(
        self, xml_file: Path, repo_name: str, seen: set[str],
    ) -> list[ConfigurationInfo]:
        """Extract configuration elements and property placeholders from XML."""
        configs: list[ConfigurationInfo] = []
        try:
            content = xml_file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("Cannot read %s: %s", xml_file, e)
            return configs

        # Extract top-level element names as config sections
        for m in _XML_ELEMENT_RE.finditer(content):
            elem = m.group(1)
            # Skip common non-config XML elements
            if elem.lower() in ("xml", "beans", "bean", "import", "property",
                                 "constructor-arg", "ref", "value", "list",
                                 "map", "set", "entry"):
                continue
            key = f"xml:{Path(xml_file).stem}.{elem}"
            if key not in seen:
                seen.add(key)
                configs.append(ConfigurationInfo(
                    config_key=key,
                    config_type="xml",
                    source_file=str(xml_file),
                    repo_name=repo_name,
                ))

        # Extract property placeholders ${...}
        for m in _XML_PLACEHOLDER_RE.finditer(content):
            key = m.group(1)
            if key not in seen:
                seen.add(key)
                configs.append(ConfigurationInfo(
                    config_key=key,
                    config_type="xml",
                    source_file=str(xml_file),
                    repo_name=repo_name,
                ))

        return configs

    def _parse_yaml_config(
        self, yaml_file: Path, repo_name: str, seen: set[str],
    ) -> list[ConfigurationInfo]:
        """Extract top-level YAML keys as configuration entries."""
        configs: list[ConfigurationInfo] = []
        try:
            content = yaml_file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("Cannot read %s: %s", yaml_file, e)
            return configs

        for m in _YAML_KEY_RE.finditer(content):
            key = m.group(1)
            if key not in seen:
                seen.add(key)
                configs.append(ConfigurationInfo(
                    config_key=key,
                    config_type="yaml",
                    source_file=str(yaml_file),
                    repo_name=repo_name,
                ))

        return configs
