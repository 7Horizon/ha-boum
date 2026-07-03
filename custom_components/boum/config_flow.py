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

from .api import BoumApi, BoumApiError, BoumAuthError
from .const import (
    CONF_DEVICE_MODEL,
    CONF_DEVICES,
    CONF_TANK_TYPE,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_TANK_TYPE,
    DOMAIN,
)

_CONF_DEVICE = "device"

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


def _device_label(devices: list[dict], device_id: str) -> str:
    for d in devices:
        if d["id"] == device_id:
            return d["name"] or f"Boum {device_id[:8]}"
    return f"Boum {device_id[:8]}"


def _device_schema(devices: list[dict]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(_CONF_DEVICE): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=d["id"], label=_device_label(devices, d["id"])
                        )
                        for d in devices
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


class BoumConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup UI for Boum.

    Flow: account credentials → pick a device from a dropdown → configure its
    tank/controller → optionally repeat for further devices.  Only devices
    that are actually configured here end up being polled.
    """

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict = {}
        self._devices: list[dict] = []
        self._configured: dict[str, dict] = {}
        self._selected: str = ""

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
                devices = await api.get_claimed_devices()
            except BoumAuthError:
                errors["base"] = "invalid_auth"
            except BoumApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
                self._abort_if_unique_id_configured()
                if not devices:
                    return self.async_abort(reason="no_devices")
                self._credentials = user_input
                self._devices = devices
                return await self.async_step_select_device()

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

    async def async_step_select_device(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._selected = user_input[_CONF_DEVICE]
            return await self.async_step_tank()

        remaining = [d for d in self._devices if d["id"] not in self._configured]
        return self.async_show_form(
            step_id="select_device",
            data_schema=_device_schema(remaining),
        )

    async def async_step_tank(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._configured[self._selected] = {
                CONF_TANK_TYPE: user_input[CONF_TANK_TYPE],
                CONF_DEVICE_MODEL: user_input[CONF_DEVICE_MODEL],
            }
            if all(d["id"] in self._configured for d in self._devices):
                return self._async_create_entry()
            return await self.async_step_add_another()

        return self.async_show_form(
            step_id="tank",
            data_schema=_tank_schema(),
            description_placeholders={
                "device_name": _device_label(self._devices, self._selected)
            },
        )

    async def async_step_add_another(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        return self.async_show_menu(
            step_id="add_another",
            menu_options=["select_device", "finish"],
        )

    async def async_step_finish(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        return self._async_create_entry()

    def _async_create_entry(self) -> config_entries.FlowResult:
        return self.async_create_entry(
            title=self._credentials[CONF_EMAIL],
            data=self._credentials,
            options={CONF_DEVICES: self._configured},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> BoumOptionsFlow:
        return BoumOptionsFlow(config_entry)


class BoumOptionsFlow(config_entries.OptionsFlow):
    """Add a device or change tank/controller of an already configured one."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._devices: list[dict] = []
        self._selected: str = ""

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        if not self._devices:
            try:
                coordinator = self.hass.data[DOMAIN][self._entry.entry_id]
                self._devices = await coordinator.api.get_claimed_devices()
            except (KeyError, BoumApiError):
                return self.async_abort(reason="cannot_connect")
            if not self._devices:
                return self.async_abort(reason="no_devices")

        if user_input is not None:
            self._selected = user_input[_CONF_DEVICE]
            return await self.async_step_tank()

        return self.async_show_form(
            step_id="init",
            data_schema=_device_schema(self._devices),
        )

    async def async_step_tank(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        configured = self._entry.options.get(CONF_DEVICES, {})

        if user_input is not None:
            devices = {
                **configured,
                self._selected: {
                    CONF_TANK_TYPE: user_input[CONF_TANK_TYPE],
                    CONF_DEVICE_MODEL: user_input[CONF_DEVICE_MODEL],
                },
            }
            return self.async_create_entry(
                title="", data={**self._entry.options, CONF_DEVICES: devices}
            )

        # Defaults follow the same chain as the coordinator: per-device →
        # legacy account-wide option → default.
        current = configured.get(self._selected, {})
        return self.async_show_form(
            step_id="tank",
            data_schema=_tank_schema(
                tank_type=current.get(
                    CONF_TANK_TYPE,
                    self._entry.options.get(CONF_TANK_TYPE, DEFAULT_TANK_TYPE),
                ),
                device_model=current.get(
                    CONF_DEVICE_MODEL,
                    self._entry.options.get(CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL),
                ),
            ),
            description_placeholders={
                "device_name": _device_label(self._devices, self._selected)
            },
        )
