"""Configuration loader for dental-lead-generator.

Loads and strictly validates seven human-editable JSON configuration files:
- config/dso_keywords.json
- config/excluded_chains.json
- config/territories.json
- config/existing_referrers.json
- config/do_not_contact.json
- config/qualification_rules.json
- config/blockers.json

Ensures:
- Required keys, data types, and allowed values are enforced.
- Does not silently fall back to empty lists when files are invalid or missing.
- Shows exact filename and invalid field upon validation failure.
- Never logs API tokens or credentials.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class ConfigValidationError(Exception):
    """Raised when JSON configuration files are missing, malformed, or fail validation."""
    pass


class DsoKeywordsConfig(BaseModel):
    version: int = 1
    description: Optional[str] = None
    match_mode: str = "case_insensitive"
    keywords: List[str] = Field(default_factory=list)
    action_on_match: str = "needs_review"


class ExcludedChainsConfig(BaseModel):
    version: int = 1
    wisdom_sales: List[str] = Field(default_factory=list)


class ZipCodeEntry(BaseModel):
    zip_code: str
    city: Optional[str] = None
    state: Optional[str] = None
    county: Optional[str] = None
    status: str = "candidate"  # "candidate" or "approved"
    source: Optional[str] = "Territory Research Template"
    notes: Optional[str] = None


class ZipCodesConfig(BaseModel):
    version: int = 1
    description: Optional[str] = None
    state_focus: Optional[str] = None
    zip_codes: List[ZipCodeEntry] = Field(default_factory=list)


class TerritoriesConfig(BaseModel):
    version: int = 1
    description: Optional[str] = None
    approved_zip_codes: List[str] = Field(default_factory=list)
    approved_cities: List[str] = Field(default_factory=list)
    approved_states: List[str] = Field(default_factory=list)
    require_approved_territory: bool = True


class EntityEntry(BaseModel):
    type: str  # 'name', 'phone', 'domain', 'place_id'
    value: str
    reason: Optional[str] = "Referral or DNC restriction"
    active: bool = True


class EntityListConfig(BaseModel):
    version: int = 1
    entries: List[EntityEntry] = Field(default_factory=list)


class CampaignRuleConfig(BaseModel):
    target_categories: List[str] = Field(default_factory=list)
    prefer_independent_practices: bool = True
    dso_match_action: str = "needs_review"
    excluded_chain_action: str = "none"


class DedupeRulesConfig(BaseModel):
    domain_match_different_address: str = "new_location"
    shared_platforms: List[str] = Field(default_factory=list)


class SearchConfig(BaseModel):
    default_search_scope: str = "zip"
    query_format_zip: str = "{keyword} in {zipcode}"
    query_format_city: str = "{keyword} in {city}"


class QualificationRulesConfig(BaseModel):
    version: int = 1
    campaigns: Dict[str, CampaignRuleConfig] = Field(default_factory=dict)
    required_google_maps_fields: List[str] = Field(
        default_factory=lambda: ["practice_name", "website", "phone", "address"]
    )
    missing_required_field_action: str = "skip"
    ambiguous_duplicate_action: str = "needs_review"
    dedupe: DedupeRulesConfig = Field(default_factory=DedupeRulesConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)


class BlockerDefinition(BaseModel):
    id: str
    enabled: bool = True
    severity: str  # "block" or "review"
    applies_to: List[str] = Field(default_factory=lambda: ["PRM", "WISDOM_SALES"])
    condition: str
    action: Optional[str] = "review"  # "skip" or "review"
    status: str = "Needs Review"
    message: str


class BlockersConfig(BaseModel):
    version: int = 1
    blockers: List[BlockerDefinition] = Field(default_factory=list)


class AppConfigBundle:
    """Container holding all loaded and validated JSON configurations."""

    def __init__(
        self,
        dso_keywords: DsoKeywordsConfig,
        excluded_chains: ExcludedChainsConfig,
        territories: TerritoriesConfig,
        existing_referrers: EntityListConfig,
        do_not_contact: EntityListConfig,
        qualification_rules: QualificationRulesConfig,
        blockers: BlockersConfig,
        zip_codes: Optional[ZipCodesConfig] = None,
    ):
        self.dso_keywords = dso_keywords
        self.excluded_chains = excluded_chains
        self.territories = territories
        self.existing_referrers = existing_referrers
        self.do_not_contact = do_not_contact
        self.qualification_rules = qualification_rules
        self.blockers = blockers
        self.zip_codes = zip_codes or ZipCodesConfig()


class ConfigLoader:
    """Loads and validates all application configuration from JSON files."""

    ALLOWED_SEVERITIES = {"block", "review"}
    ALLOWED_ENTITY_TYPES = {"name", "phone", "domain", "place_id"}
    ALLOWED_ACTIONS = {"needs_review", "disqualify", "none"}

    KNOWN_CONDITIONS = {
        "no_approved_territory",
        "missing_practice_name",
        "missing_address",
        "missing_phone",
        "missing_website",
        "dso_keyword_match",
        "existing_referrer_match",
        "do_not_contact_match",
        "duplicate_match",
        "chain_exclusion_match",
    }

    def __init__(self, config_dir: Optional[Path] = None):
        if config_dir is None:
            self.config_dir = Path(__file__).parent.parent / "config"
        else:
            self.config_dir = Path(config_dir)

    def _read_json_file(self, filename: str) -> Dict[str, Any]:
        """Read and parse a JSON file, raising informative error if missing or invalid."""
        filepath = self.config_dir / filename
        if not filepath.exists():
            raise ConfigValidationError(
                f"Configuration file missing: '{filepath.name}' in directory '{self.config_dir}'. "
                "Automatic fallback is disabled to prevent unsafe execution."
            )
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    raise ConfigValidationError(
                        f"Configuration file '{filename}' must contain a JSON object at root, got {type(data).__name__}."
                    )
                return data
        except json.JSONDecodeError as e:
            raise ConfigValidationError(
                f"Invalid JSON syntax in '{filename}' at line {e.lineno}, col {e.colno}: {e.msg}"
            ) from e
        except Exception as e:
            raise ConfigValidationError(f"Error reading configuration file '{filename}': {e}") from e

    def load_dso_keywords(self) -> DsoKeywordsConfig:
        raw = self._read_json_file("dso_keywords.json")
        try:
            cfg = DsoKeywordsConfig(**raw)
            if not isinstance(cfg.keywords, list):
                raise ConfigValidationError("Field 'keywords' in 'dso_keywords.json' must be a list.")
            return cfg
        except ValidationError as e:
            raise ConfigValidationError(f"Validation error in 'dso_keywords.json': {e}") from e

    def load_excluded_chains(self) -> ExcludedChainsConfig:
        raw = self._read_json_file("excluded_chains.json")
        try:
            cfg = ExcludedChainsConfig(**raw)
            if not isinstance(cfg.wisdom_sales, list):
                raise ConfigValidationError("Field 'wisdom_sales' in 'excluded_chains.json' must be a list.")
            return cfg
        except ValidationError as e:
            raise ConfigValidationError(f"Validation error in 'excluded_chains.json': {e}") from e

    def load_territories(self) -> TerritoriesConfig:
        raw = self._read_json_file("territories.json")
        try:
            cfg = TerritoriesConfig(**raw)
            # Ensure ZIP codes are strings
            for zip_code in cfg.approved_zip_codes:
                if not isinstance(zip_code, str):
                    raise ConfigValidationError(
                        f"Invalid ZIP code '{zip_code}' in 'territories.json': ZIP codes must be strings to preserve leading zeros."
                    )
            return cfg
        except ValidationError as e:
            raise ConfigValidationError(f"Validation error in 'territories.json': {e}") from e

    def load_entity_list(self, filename: str) -> EntityListConfig:
        raw = self._read_json_file(filename)
        try:
            cfg = EntityListConfig(**raw)
            for idx, entry in enumerate(cfg.entries):
                if entry.type not in self.ALLOWED_ENTITY_TYPES:
                    raise ConfigValidationError(
                        f"Invalid entity type '{entry.type}' at index {idx} in '{filename}'. "
                        f"Allowed types: {sorted(list(self.ALLOWED_ENTITY_TYPES))}"
                    )
                if not entry.value or not str(entry.value).strip():
                    raise ConfigValidationError(
                        f"Missing or empty 'value' field at entry index {idx} in '{filename}'."
                    )
            return cfg
        except ValidationError as e:
            raise ConfigValidationError(f"Validation error in '{filename}': {e}") from e

    def load_qualification_rules(self) -> QualificationRulesConfig:
        raw = self._read_json_file("qualification_rules.json")
        try:
            cfg = QualificationRulesConfig(**raw)
            for campaign_name, camp_cfg in cfg.campaigns.items():
                if camp_cfg.dso_match_action not in self.ALLOWED_ACTIONS:
                    raise ConfigValidationError(
                        f"Invalid dso_match_action '{camp_cfg.dso_match_action}' for campaign '{campaign_name}' "
                        f"in 'qualification_rules.json'. Allowed: {self.ALLOWED_ACTIONS}"
                    )
                if camp_cfg.excluded_chain_action not in self.ALLOWED_ACTIONS:
                    raise ConfigValidationError(
                        f"Invalid excluded_chain_action '{camp_cfg.excluded_chain_action}' for campaign '{campaign_name}' "
                        f"in 'qualification_rules.json'. Allowed: {self.ALLOWED_ACTIONS}"
                    )
            return cfg
        except ValidationError as e:
            raise ConfigValidationError(f"Validation error in 'qualification_rules.json': {e}") from e

    def load_blockers(self) -> BlockersConfig:
        raw = self._read_json_file("blockers.json")
        try:
            cfg = BlockersConfig(**raw)
            seen_ids: Set[str] = set()
            for b in cfg.blockers:
                if b.id in seen_ids:
                    raise ConfigValidationError(
                        f"Duplicate blocker ID '{b.id}' detected in 'blockers.json'. All blocker IDs must be unique."
                    )
                seen_ids.add(b.id)

                if b.severity not in self.ALLOWED_SEVERITIES:
                    raise ConfigValidationError(
                        f"Invalid severity '{b.severity}' for blocker '{b.id}' in 'blockers.json'. "
                        f"Allowed values: {sorted(list(self.ALLOWED_SEVERITIES))}"
                    )

                if b.condition not in self.KNOWN_CONDITIONS:
                    raise ConfigValidationError(
                        f"Unknown blocker condition '{b.condition}' for blocker '{b.id}' in 'blockers.json'. "
                        f"Unknown conditions are rejected for safety. Allowed conditions: {sorted(list(self.KNOWN_CONDITIONS))}"
                    )
            return cfg
        except ValidationError as e:
            raise ConfigValidationError(f"Validation error in 'blockers.json': {e}") from e

    def load_zip_codes(self) -> ZipCodesConfig:
        filepath = self.config_dir / "zip_codes.json"
        if not filepath.exists():
            # If not present, return empty config safely
            return ZipCodesConfig()
        raw = self._read_json_file("zip_codes.json")
        try:
            cfg = ZipCodesConfig(**raw)
            for z in cfg.zip_codes:
                if not isinstance(z.zip_code, str):
                    raise ConfigValidationError(
                        f"Invalid zip_code '{z.zip_code}' in 'zip_codes.json': ZIP codes must be strings to preserve leading zeros."
                    )
            return cfg
        except ValidationError as e:
            raise ConfigValidationError(f"Validation error in 'zip_codes.json': {e}") from e

    ALLOWLISTED_RESOURCES = {
        "dso_keywords": "dso_keywords.json",
        "excluded_chains": "excluded_chains.json",
        "territories": "territories.json",
        "existing_referrers": "existing_referrers.json",
        "do_not_contact": "do_not_contact.json",
        "qualification_rules": "qualification_rules.json",
        "blockers": "blockers.json",
        "zip_codes": "zip_codes.json",
        "airtable_schema": "airtable_schema.json",
    }

    def get_resource_raw(self, resource_name: str) -> Dict[str, Any]:
        """Fetch raw JSON for an allowlisted configuration resource."""
        if resource_name not in self.ALLOWLISTED_RESOURCES:
            raise ConfigValidationError(
                f"Unknown configuration resource '{resource_name}'. Allowed: {sorted(list(self.ALLOWLISTED_RESOURCES.keys()))}"
            )
        filename = self.ALLOWLISTED_RESOURCES[resource_name]
        return self._read_json_file(filename)

    def validate_resource_data(self, resource_name: str, data: Dict[str, Any]) -> Any:
        """Validate raw dictionary for a given configuration resource against its model."""
        if resource_name not in self.ALLOWLISTED_RESOURCES:
            raise ConfigValidationError(
                f"Unknown configuration resource '{resource_name}'. Allowed: {sorted(list(self.ALLOWLISTED_RESOURCES.keys()))}"
            )

        try:
            if resource_name == "dso_keywords":
                cfg = DsoKeywordsConfig(**data)
                if not isinstance(cfg.keywords, list):
                    raise ConfigValidationError("Field 'keywords' must be a list.")
                return cfg
            elif resource_name == "excluded_chains":
                cfg = ExcludedChainsConfig(**data)
                if not isinstance(cfg.wisdom_sales, list):
                    raise ConfigValidationError("Field 'wisdom_sales' must be a list.")
                return cfg
            elif resource_name == "territories":
                cfg = TerritoriesConfig(**data)
                for zip_code in cfg.approved_zip_codes:
                    if not isinstance(zip_code, str):
                        raise ConfigValidationError("All entries in 'approved_zip_codes' must be strings.")
                return cfg
            elif resource_name in ("existing_referrers", "do_not_contact"):
                cfg = EntityListConfig(**data)
                for idx, entry in enumerate(cfg.entries):
                    if entry.type not in self.ALLOWED_ENTITY_TYPES:
                        raise ConfigValidationError(
                            f"Invalid entity type '{entry.type}' at index {idx}. Allowed: {sorted(list(self.ALLOWED_ENTITY_TYPES))}"
                        )
                    if not entry.value or not str(entry.value).strip():
                        raise ConfigValidationError(f"Missing or empty 'value' field at entry index {idx}.")
                return cfg
            elif resource_name == "qualification_rules":
                cfg = QualificationRulesConfig(**data)
                for campaign_name, camp_cfg in cfg.campaigns.items():
                    if camp_cfg.dso_match_action not in self.ALLOWED_ACTIONS:
                        raise ConfigValidationError(
                            f"Invalid dso_match_action '{camp_cfg.dso_match_action}' for campaign '{campaign_name}'. Allowed: {self.ALLOWED_ACTIONS}"
                        )
                    if camp_cfg.excluded_chain_action not in self.ALLOWED_ACTIONS:
                        raise ConfigValidationError(
                            f"Invalid excluded_chain_action '{camp_cfg.excluded_chain_action}' for campaign '{campaign_name}'. Allowed: {self.ALLOWED_ACTIONS}"
                        )
                return cfg
            elif resource_name == "blockers":
                cfg = BlockersConfig(**data)
                seen_ids: Set[str] = set()
                for b in cfg.blockers:
                    if b.id in seen_ids:
                        raise ConfigValidationError(f"Duplicate blocker ID '{b.id}' detected in 'blockers.json'.")
                    seen_ids.add(b.id)
                    if b.severity not in self.ALLOWED_SEVERITIES:
                        raise ConfigValidationError(
                            f"Invalid severity '{b.severity}' for blocker '{b.id}'. Allowed: {sorted(list(self.ALLOWED_SEVERITIES))}"
                        )
                    if b.condition not in self.KNOWN_CONDITIONS:
                        raise ConfigValidationError(
                            f"Unknown blocker condition '{b.condition}' for blocker '{b.id}'. Allowed: {sorted(list(self.KNOWN_CONDITIONS))}"
                        )
                return cfg
            elif resource_name == "zip_codes":
                cfg = ZipCodesConfig(**data)
                for z in cfg.zip_codes:
                    if not isinstance(z.zip_code, str):
                        raise ConfigValidationError(f"Invalid zip_code '{z.zip_code}': ZIP codes must be strings to preserve leading zeros.")
                return cfg
            elif resource_name == "airtable_schema":
                if not isinstance(data, dict):
                    raise ConfigValidationError("Field 'airtable_schema' must be a valid JSON dictionary.")
                return BaseModel()
        except ValidationError as e:
            raise ConfigValidationError(f"Schema validation error in '{resource_name}': {e}") from e

    def save_resource_data(self, resource_name: str, data: Dict[str, Any]) -> None:
        """Validate and atomically write configuration data to file."""
        # 1. Validate data schema strictly
        validated_model = self.validate_resource_data(resource_name, data)

        filename = self.ALLOWLISTED_RESOURCES[resource_name]
        target_path = self.config_dir / filename
        temp_path = self.config_dir / f"{filename}.tmp"

        # 2. Write to temp file then atomic rename
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(validated_model.model_dump(), f, indent=2)
                f.write("\n")
            temp_path.replace(target_path)
            logger.info(f"Successfully saved and validated resource '{resource_name}' to '{target_path.name}'.")
        except Exception as e:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise ConfigValidationError(f"Failed to persist configuration file '{filename}': {e}") from e

    def load_all(self) -> AppConfigBundle:
        """Load and strictly validate all JSON files."""
        logger.info(f"Loading JSON configuration from: {self.config_dir}")
        bundle = AppConfigBundle(
            dso_keywords=self.load_dso_keywords(),
            excluded_chains=self.load_excluded_chains(),
            territories=self.load_territories(),
            existing_referrers=self.load_entity_list("existing_referrers.json"),
            do_not_contact=self.load_entity_list("do_not_contact.json"),
            qualification_rules=self.load_qualification_rules(),
            blockers=self.load_blockers(),
            zip_codes=self.load_zip_codes(),
        )
        logger.info("Successfully loaded and validated all JSON configuration files.")
        return bundle
