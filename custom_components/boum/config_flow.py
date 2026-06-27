"""Config flow for the Boum integration."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import BoumApi, BoumAuthError, BoumApiError
from .const import (
    CONF_DEVICE_MODEL,
    CONF_TANK_TYPE,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_TANK_TYPE,
    DOMAIN,
)

_TANK_OPTIONS = [
    SelectOptionDict(value="35l", label="35 Liter (Boum 2 / Boum 3)"),
    SelectOptionDict(value="55l", label="55 Liter (Boum 2 / Boum 3)"),
    SelectOptionDict(value="32l", label="32 Liter (Boum Core)"),
]

_DEVICE_OPTIONS = [
    SelectOptionDict(value="boum_2", label="Boum 2"),
    SelectOptionDict(value="boum_3", label="Boum 3"),
    SelectOptionDict(value="boum_core", label="Boum Core"),
]


def _tank_schema(
    tank_type: str = DEFAULT_TANK_TYPE,
    device_model: str = DEFAULT_DEVICE_MODEL,
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_TANK_TYPE, default=tank_type): SelectSelector(
                SelectSelectorConfig(
                    options=_TANK_OPTIONS,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(CONF_DEVICE_MODEL, default=device_model): SelectSelector(
                SelectSelectorConfig(
                    options=_DEVICE_OPTIONS,
                    mode=SelectSelectorMode.LIST,
                )
            ),
        }
    )


class BoumConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup UI for Boum."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict = {}

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = BoumApi(
                email=user_input[CONF_EMAIL],
                password=user_input[CONF_PASSWORD],
                session=session,
            )
            try:
                await api.authenticate()
            except BoumAuthError:
                errors["base"] = "invalid_auth"
            except BoumApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
                self._abort_if_unique_id_configured()
                self._credentials = user_input
                return await self.async_step_tank()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_tank(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=self._credentials[CONF_EMAIL],
                data=self._credentials,
                options={
                    CONF_TANK_TYPE: user_input[CONF_TANK_TYPE],
                    CONF_DEVICE_MODEL: user_input[CONF_DEVICE_MODEL],
                },
            )

        return self.async_show_form(
            step_id="tank",
            data_schema=_tank_schema(),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> BoumOptionsFlow:
        return BoumOptionsFlow(config_entry)


class BoumOptionsFlow(config_entries.OptionsFlow):
    """Allow changing tank type and device model after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=_tank_schema(
                tank_type=current.get(CONF_TANK_TYPE, DEFAULT_TANK_TYPE),
                device_model=current.get(CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL),
            ),
        )
