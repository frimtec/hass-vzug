import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from yarl import URL

from . import api
from .const import CONF_BASE_URL, DOMAIN
from .shared import Shared

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up V-ZUG from a config entry."""
    base_url = URL(entry.data[CONF_BASE_URL])
    try:
        credentials = api.Credentials(
            username=entry.data[CONF_USERNAME], password=entry.data[CONF_PASSWORD]
        )
    except KeyError:
        credentials = None
    shared = Shared(hass, base_url, credentials)
    await shared.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = shared

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        shared: Shared
        if shared := hass.data[DOMAIN].pop(entry.entry_id):
            await shared.async_shutdown()

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    _LOGGER.debug("migrating from version %s.%s", config_entry.version, config_entry.minor_version)

    if config_entry.version == 2 and config_entry.minor_version == 2:
        return False

    new_data = config_entry.data.copy()

    if config_entry.version == 1:
        # migrate base_url
        base_url = URL(config_entry.data["host"])
        if not base_url.is_absolute():
            base_url = URL(f"http://{base_url}")
        new_data[CONF_BASE_URL] = str(base_url)

    if config_entry.minor_version == 1:
        # migrate old entity unique id
        entity_reg = er.async_get(hass)
        entities: list[er.RegistryEntry] = er.async_entries_for_config_entry(
            entity_reg, config_entry.entry_id
        )

        # setup coordinator to get required data for unique_id
        try:
            credentials = api.Credentials(
                username=config_entry.data[CONF_USERNAME], password=config_entry.data[CONF_PASSWORD]
            )
        except KeyError:
            credentials = None
        base_url = URL(new_data[CONF_BASE_URL])
        shared = Shared(hass, base_url, credentials)
        await shared.async_config_entry_first_refresh()
        old_prefix = shared.state_coord.data.device.get("deviceUuid", "") or shared.state_coord.data.device.get("Serial", "")
        mac_addr = dr.format_mac(shared.meta.mac_address)

        for entity in entities:
            # migrate unique_id_prefix from 'device_uuid' or 'device_serial' to 'mac_addr'

            if old_prefix not in entity.unique_id:
                continue

            new_uid = entity.unique_id.replace(old_prefix, mac_addr)
            _LOGGER.debug(
                "migrate unique id '%s' to '%s'", entity.unique_id, new_uid
            )
            entity_reg.async_update_entity(
                entity.entity_id, new_unique_id=new_uid
            )

        hass.config_entries.async_update_entry(
            config_entry, data={"base_url": str(base_url)}, version = 2, minor_version = 2
        )

    _LOGGER.debug("migration to version %s.%s successful", config_entry.version, config_entry.minor_version)

    return True
