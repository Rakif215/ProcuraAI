"""
Backend mail-provider discovery for Orion Lite onboarding.

Discovery order:
1. Orion internal preset registry (verified pilot-safe presets)
2. Domain autoconfig endpoints
3. Local ISPDB-style fallback data
4. Manual advanced setup
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ProviderConfig:
    provider_id: str
    display_name: str
    domain: str
    source: str
    auth_type: str  # oauth | password | manual
    imap_host: str = ""
    imap_port: int = 993
    imap_security: str = "ssl"
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = "ssl"
    username_strategy: str = "email"  # email | localpart | custom
    username_label: str = "Email address"
    help_text: str = ""
    requires_manual: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


PROVIDER_TYPES: dict[str, ProviderConfig] = {
    "gmail": ProviderConfig(
        provider_id="gmail",
        display_name="Gmail",
        domain="gmail.com",
        source="registry",
        auth_type="oauth",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_security="ssl",
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_security="starttls",
        username_strategy="email",
        help_text="Connect with Google. Orion stores encrypted tokens instead of your Google password.",
    ),
    "outlook": ProviderConfig(
        provider_id="outlook",
        display_name="Outlook / Microsoft",
        domain="outlook.com",
        source="registry",
        auth_type="password",
        imap_host="outlook.office365.com",
        imap_port=993,
        imap_security="ssl",
        smtp_host="smtp.office365.com",
        smtp_port=587,
        smtp_security="starttls",
        username_strategy="email",
        help_text="Use your Microsoft mailbox password or an app password if your account requires two-step verification.",
    ),
    "spacemail": ProviderConfig(
        provider_id="spacemail",
        display_name="SpaceMail",
        domain="mafaz.me",
        source="registry",
        auth_type="password",
        imap_host="mail.spacemail.com",
        imap_port=993,
        imap_security="ssl",
        smtp_host="mail.spacemail.com",
        smtp_port=465,
        smtp_security="ssl",
        username_strategy="email",
        help_text="Use your SpaceMail mailbox password.",
    ),
    "fau": ProviderConfig(
        provider_id="fau",
        display_name="FAU / RRZE Exchange",
        domain="fau.de",
        source="registry",
        auth_type="password",
        imap_host="groupware.fau.de",
        imap_port=993,
        imap_security="ssl",
        smtp_host="groupware.fau.de",
        smtp_port=587,
        smtp_security="starttls",
        username_strategy="custom",
        username_label="FAU username",
        help_text="FAU may require your IdM or Exchange username instead of the full email address.",
    ),
    "yahoo": ProviderConfig(
        provider_id="yahoo",
        display_name="Yahoo Mail",
        domain="yahoo.com",
        source="ispdb_local",
        auth_type="password",
        imap_host="imap.mail.yahoo.com",
        imap_port=993,
        imap_security="ssl",
        smtp_host="smtp.mail.yahoo.com",
        smtp_port=465,
        smtp_security="ssl",
        username_strategy="email",
        help_text="Use your Yahoo mailbox password or app password if required.",
    ),
}


DOMAIN_REGISTRY: dict[str, str] = {
    "gmail.com": "gmail",
    "googlemail.com": "gmail",
    "outlook.com": "outlook",
    "hotmail.com": "outlook",
    "live.com": "outlook",
    "office365.com": "outlook",
    "outlook.office365.com": "outlook",
    "mafaz.me": "spacemail",
    "fau.de": "fau",
    "rrze.uni-erlangen.de": "fau",
    "yahoo.com": "yahoo",
}


PROVIDER_HINTS: dict[str, str] = {
    "gmail": "gmail",
    "google": "gmail",
    "outlook": "outlook",
    "microsoft": "outlook",
    "office365": "outlook",
    "spacemail": "spacemail",
    "space-mail": "spacemail",
    "fau": "fau",
    "rrze": "fau",
    "yahoo": "yahoo",
    "custom": "custom",
    "manual": "custom",
}


ISPDB_FILE = Path(__file__).resolve().parent.parent / "data" / "mail_provider_ispdb.json"


def _clone_provider(provider_id: str, domain: str, source: str) -> ProviderConfig:
    template = PROVIDER_TYPES[provider_id]
    return replace(template, domain=domain, source=source)


def _load_local_ispdb() -> dict[str, ProviderConfig]:
    if not ISPDB_FILE.exists():
        return {}
    try:
        raw = json.loads(ISPDB_FILE.read_text())
    except Exception as exc:
        logger.warning("Could not load local ISPDB data: %s", exc)
        return {}

    loaded: dict[str, ProviderConfig] = {}
    for domain, config in raw.items():
        try:
            loaded[domain.lower()] = ProviderConfig(
                provider_id=config["provider_id"],
                display_name=config["display_name"],
                domain=domain.lower(),
                source="ispdb_local",
                auth_type=config.get("auth_type", "password"),
                imap_host=config.get("imap_host", ""),
                imap_port=int(config.get("imap_port", 993)),
                imap_security=config.get("imap_security", "ssl"),
                smtp_host=config.get("smtp_host", ""),
                smtp_port=int(config.get("smtp_port", 465)),
                smtp_security=config.get("smtp_security", "ssl"),
                username_strategy=config.get("username_strategy", "email"),
                username_label=config.get("username_label", "Email address"),
                help_text=config.get("help_text", "Orion found a local provider profile for this domain."),
                requires_manual=bool(config.get("requires_manual", False)),
            )
        except Exception as exc:
            logger.warning("Skipping invalid ISPDB entry for %s: %s", domain, exc)
    return loaded


LOCAL_ISPDB_STYLE = _load_local_ispdb()


def _parse_security(value: str) -> str:
    lowered = (value or "").strip().lower()
    if lowered in {"ssl", "ssl/tls", "tls"}:
        return "ssl"
    if lowered == "starttls":
        return "starttls"
    return "plain"


def _find_first_tag(root: ET.Element, tag_name: str) -> Optional[ET.Element]:
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == tag_name:
            return node
    return None


def _find_servers(root: ET.Element, tag_name: str, server_type: str) -> list[ET.Element]:
    matches: list[ET.Element] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != tag_name:
            continue
        if (node.attrib.get("type") or "").lower() == server_type:
            matches.append(node)
    return matches


def _child_text(node: ET.Element, tag_name: str, default: str = "") -> str:
    for child in node:
        if child.tag.rsplit("}", 1)[-1] == tag_name and child.text:
            return child.text.strip()
    return default


def _normalize_xml_config(root: ET.Element, domain: str, source: str) -> Optional[ProviderConfig]:
    incoming_servers = _find_servers(root, "incomingServer", "imap")
    outgoing_servers = _find_servers(root, "outgoingServer", "smtp")
    if not incoming_servers or not outgoing_servers:
        return None

    incoming = incoming_servers[0]
    outgoing = outgoing_servers[0]

    username = _child_text(incoming, "username", "%EMAILADDRESS%")
    username_strategy = "email"
    username_label = "Email address"
    if username == "%EMAILLOCALPART%":
        username_strategy = "localpart"
        username_label = "Email username"
    elif username not in {"%EMAILADDRESS%", ""}:
        username_strategy = "custom"
        username_label = "Mailbox username"

    display_name = domain
    provider_node = _find_first_tag(root, "displayName")
    if provider_node is not None and provider_node.text:
        display_name = provider_node.text.strip()

    return ProviderConfig(
        provider_id=domain,
        display_name=display_name,
        domain=domain,
        source=source,
        auth_type="password",
        imap_host=_child_text(incoming, "hostname"),
        imap_port=int(_child_text(incoming, "port", "993") or 993),
        imap_security=_parse_security(_child_text(incoming, "socketType", "SSL")),
        smtp_host=_child_text(outgoing, "hostname"),
        smtp_port=int(_child_text(outgoing, "port", "465") or 465),
        smtp_security=_parse_security(_child_text(outgoing, "socketType", "SSL")),
        username_strategy=username_strategy,
        username_label=username_label,
        help_text="Orion found this provider configuration automatically from your domain.",
    )


def _fetch_domain_autoconfig(domain: str) -> Optional[ProviderConfig]:
    urls = [
        f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml",
        f"https://autoconfig.{domain}/mail/config-v1.1.xml",
    ]
    with httpx.Client(timeout=8.0, follow_redirects=True) as client:
        for url in urls:
            try:
                response = client.get(url)
                if response.status_code != 200 or not response.text.strip():
                    continue
                root = ET.fromstring(response.text)
                config = _normalize_xml_config(root, domain, "autoconfig")
                if config:
                    return config
            except Exception as exc:
                logger.debug("Autoconfig lookup failed for %s: %s", url, exc)
    return None


def fallback_manual_config(domain: str) -> ProviderConfig:
    return ProviderConfig(
        provider_id="custom",
        display_name="Manual setup",
        domain=domain,
        source="fallback",
        auth_type="manual",
        username_strategy="email",
        help_text="Orion could not verify your provider settings automatically. You can continue with manual advanced setup.",
        requires_manual=True,
    )


def _registry_lookup_by_domain(domain: str) -> Optional[ProviderConfig]:
    provider_id = DOMAIN_REGISTRY.get(domain)
    if not provider_id:
        return None
    return _clone_provider(provider_id, domain, "registry")


def _registry_lookup_by_hint(provider_hint: Optional[str], domain: str) -> Optional[ProviderConfig]:
    if not provider_hint:
        return None
    normalized = provider_hint.strip().lower()

    if normalized in DOMAIN_REGISTRY:
        return _registry_lookup_by_domain(normalized)

    provider_id = PROVIDER_HINTS.get(normalized)
    if provider_id and provider_id in PROVIDER_TYPES:
        return _clone_provider(provider_id, domain or PROVIDER_TYPES[provider_id].domain, "registry")
    return None


def discover_provider_config(email: str, provider_hint: Optional[str] = None) -> ProviderConfig:
    domain = (email.split("@", 1)[1] if "@" in email else "").strip().lower()
    if not domain:
        return fallback_manual_config("")

    registry_hint = _registry_lookup_by_hint(provider_hint, domain)
    if registry_hint:
        return registry_hint

    registry_domain = _registry_lookup_by_domain(domain)
    if registry_domain:
        return registry_domain

    autoconfig = _fetch_domain_autoconfig(domain)
    if autoconfig:
        return autoconfig

    ispdb = LOCAL_ISPDB_STYLE.get(domain)
    if ispdb:
        return replace(ispdb, domain=domain, source="ispdb_local")

    return fallback_manual_config(domain)
