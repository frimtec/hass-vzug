import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict, cast

import aiohttp
import yarl

_LOGGER = logging.getLogger(__name__)

# list of status codes that we don't count as false-positives
_TRUSTED_STATUS_CODES: set[int] = {401, 404}

DeviceStatusInactiveT = Literal["true"] | Literal["false"]


class DeviceStatusProgramEnd(TypedDict, total=False):
    EndType: str
    End: str


class DeviceStatus(TypedDict, total=False):
    DeviceName: str
    Serial: str
    Inactive: DeviceStatusInactiveT
    Program: str
    Status: str
    ProgramEnd: DeviceStatusProgramEnd
    deviceUuid: str


class UpdateProgress(TypedDict, total=False):
    download: int
    installation: int


class UpdateComponent(TypedDict, total=False):
    name: str
    running: bool
    available: bool
    required: bool
    progress: UpdateProgress


class UpdateStatus(TypedDict, total=False):
    status: Literal["idle"] | str
    isAIUpdateAvailable: bool
    isHHGUpdateAvailable: bool
    isSynced: bool
    components: list[UpdateComponent]


class PushNotification(TypedDict, total=False):
    date: str
    message: str


class Command(TypedDict, total=False):
    type: Literal["action"] | Literal["boolean"] | Literal["selection"] | Literal[
        "status"
    ] | Literal["range"]
    description: str
    command: str
    value: str
    alterable: bool
    options: list[str]
    minMax: tuple[str, str]
    refresh: list[str]
    """list of commands to refresh when this command is changed"""


HhFwVersion = TypedDict(
    "HhFwVersion",
    {
        "fn": str,
        "an": str,
        "v": str,
        "vr01": str,
        "v2": str,
        "vr10": str,
        "vi2": str,
        "vh1": str,
        "vh2": str,
        "vr0B": str,
        "vp": str,
        "vr0C": str,
        "vr0E": str,
        "Mh": str,
        "MD": str,
        "Zh": str,
        "ZV": str,
        "ZHSW": str,
        "device-type": str,
    },
    total=False,
)


class AiFwVersion(TypedDict, total=False):
    fn: str
    SW: str
    SD: str
    HW: str
    apiVersion: str
    phy: str
    deviceUuid: str


class EcoInfoMetric(TypedDict, total=False):
    total: float
    average: float
    program: float
    option: float  # sent by adorawash for water, no idea what it is


class EcoInfo(TypedDict, total=False):
    water: EcoInfoMetric
    energy: EcoInfoMetric


class Category(TypedDict, total=False):
    description: str


class DeviceInfo(TypedDict, total=False):
    model: str
    description: str
    """model description"""
    type: Literal["WA"] | str
    name: str
    serialNumber: str
    articleNumber: str
    """the serial number starts with this"""
    apiVersion: str  # seen: 1.7.0 / 1.8.0
    zhMode: int


class ProgramOptionA(TypedDict, total=False):
    min: int
    max: int
    step: int


class ProgramOptionB(TypedDict, total=False):
    set: bool
    options: list[Any]


class ProgramOption(ProgramOptionA, ProgramOptionB):
    ...


class ProgramInfo(TypedDict, total=False):
    id: int
    name: str
    status: Literal["selected"] | str
    stepIds: list[int]


@dataclasses.dataclass(slots=True, kw_only=True)
class Program:
    info: ProgramInfo
    options: dict[str, ProgramOption]

    @classmethod
    def build(cls, raw: dict[str, Any]) -> "Program":
        info = {}
        options = raw.copy()
        for key in ProgramInfo.__required_keys__ | ProgramInfo.__optional_keys__:
            # extract all ProgramInfo keys from 'options' to 'info'
            try:
                info[key] = options[key]
            except LookupError:
                pass
            else:
                del options[key]
        return Program(info=cast(ProgramInfo, info), options=options)


@dataclasses.dataclass(slots=True, kw_only=True)
class AggState:
    zh_mode: int
    device: DeviceStatus
    device_fetched_at: datetime
    notifications: list[PushNotification]
    eco_info: EcoInfo


@dataclasses.dataclass(slots=True, kw_only=True)
class AggUpdateStatus:
    update: UpdateStatus
    ai_fw_version: AiFwVersion
    hh_fw_version: HhFwVersion


@dataclasses.dataclass(slots=True, kw_only=True)
class AggMeta:
    mac_address: str
    model_description: str


@dataclasses.dataclass(slots=True, kw_only=True)
class AggCategory:
    key: str
    description: str
    commands: dict[str, Command]


AggConfig = dict[str, AggCategory]


class VZugApi:
    @property
    def base_url(self) -> yarl.URL:
        return self._base_url

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: yarl.URL | str,
    ) -> None:
        self._session = session
        self._base_url = yarl.URL(base_url)
        self._sem = asyncio.Semaphore(value=3)

    async def _command(
        self,
        component: str,
        *,
        command: str,
        params: dict[str, str] | None = None,
        raw: bool = False,
        expected_type: Any = None,
        reject_empty: bool = False,
        attempts: int = 5,
        retry_delay: float = 0.5,
        value_on_err: Callable[[], Any] | None = None,
    ) -> Any:
        if params is None:
            params = {}
        final_params = params.copy()
        final_params["command"] = command
        final_params["_"] = str(int(time.time()))

        url = self._base_url / component

        async def once() -> Any:
            _LOGGER.debug(
                "running command %s %s on %s @ %s",
                command,
                params,
                component,
                self._base_url,
            )
            async with self._session.get(url, params=final_params) as resp:
                resp.raise_for_status()

                if raw:
                    content = await resp.text()
                    _LOGGER.debug("raw response: %s", content)
                    return content

                data = await resp.json(content_type=None)
                _LOGGER.debug("data: %s", data)
                if expected_type is list and data is None:
                    # if we want a list and the response is null, we just treat that as an empty list
                    data: Any = []

                if expected_type is not None:
                    assert isinstance(
                        data, expected_type
                    ), f"data type mismatch ({type(data)} != {expected_type})"
                if reject_empty:
                    assert len(data) > 0, "empty response rejected"
                return data

        last_exc = ValueError("no attempts made")
        async with self._sem:
            for _ in range(attempts):
                try:
                    return await once()
                except aiohttp.ClientResponseError as exc:
                    last_exc = exc
                    _LOGGER.debug(f"response error: {exc}")
                    if exc.status in _TRUSTED_STATUS_CODES:
                        break
                except aiohttp.ClientError as exc:
                    last_exc = exc
                    _LOGGER.debug(f"client error: {exc}")
                except AssertionError as exc:
                    last_exc = exc
                    _LOGGER.debug(f"response data assertion failed: {exc}")
                await asyncio.sleep(retry_delay)
                retry_delay *= 2.0

        if value_on_err:
            _LOGGER.exception("command error, using default", exc_info=last_exc)
            return value_on_err()

        raise last_exc

    async def aggregate_state(self, *, default_on_error: bool = True) -> AggState:
        # always start with zh_mode, that seems to do something??
        zh_mode = await self.get_zh_mode(default_on_error=True)

        device = await self.get_device_status(default_on_error=default_on_error)
        device_fetched_at = datetime.now(timezone.utc)

        return AggState(
            zh_mode=zh_mode,
            device=device,
            device_fetched_at=device_fetched_at,
            notifications=await self.get_last_push_notifications(
                default_on_error=default_on_error
            ),
            eco_info=await self.get_eco_info(default_on_error=default_on_error),
        )

    async def aggregate_update_status(
        self, *, default_on_error: bool = True
    ) -> AggUpdateStatus:
        return AggUpdateStatus(
            update=await self.get_update_status(default_on_error=default_on_error),
            ai_fw_version=await self.get_ai_fw_version(
                default_on_error=default_on_error
            ),
            hh_fw_version=await self.get_hh_fw_version(
                default_on_error=default_on_error
            ),
        )

    async def aggregate_meta(self, *, default_on_error: bool = False) -> AggMeta:
        return AggMeta(
            mac_address=await self.get_mac_address(default_on_error=default_on_error),
            model_description=await self.get_model_description(
                default_on_error=default_on_error
            ),
        )

    async def aggregate_config(self) -> AggConfig:
        category_keys = await self.list_categories()
        config_tree: AggConfig = {}
        for category_key in category_keys:
            category_raw, command_keys = asyncio.gather(
                self.get_category(category_key),
                self.list_commands(category_key),
            )
            category = AggCategory(
                key=category_key,
                description=category_raw.get("description", ""),
                commands={},
            )

            async def handle_command_key(command_key: str) -> None:
                command_raw = await self.get_command(command_key)
                category.commands[command_key] = command_raw

            await asyncio.gather(
                *(handle_command_key(command_key) for command_key in command_keys)
            )
            config_tree[category_key] = category
        return config_tree

    async def get_mac_address(self, *, default_on_error: bool = False) -> str:
        return await self._command(
            "ai",
            command="getMacAddress",
            raw=True,
            value_on_err=(lambda: "") if default_on_error else None,
        )

    async def get_model_description(self, *, default_on_error: bool = False) -> str:
        return await self._command(
            "ai",
            command="getModelDescription",
            raw=True,
            value_on_err=(lambda: "") if default_on_error else None,
        )

    async def get_device_status(
        self, *, default_on_error: bool = False
    ) -> DeviceStatus:
        return await self._command(
            "ai",
            command="getDeviceStatus",
            expected_type=dict,
            value_on_err=(lambda: DeviceStatus()) if default_on_error else None,
        )

    async def get_update_status(
        self, *, default_on_error: bool = False
    ) -> UpdateStatus:
        return await self._command(
            "ai",
            command="getUpdateStatus",
            expected_type=dict,
            value_on_err=(lambda: UpdateStatus()) if default_on_error else None,
        )

    async def check_for_updates(self) -> None:
        await self._command(
            "ai",
            command="checkUpdate",
            raw=True,
            attempts=2,
        )

    async def do_ai_update(self) -> None:
        await self._command("ai", command="doAIUpdate")

    async def do_hhg_update(self) -> None:
        await self._command("ai", command="doHHGUpdate")

    async def get_last_push_notifications(
        self, *, default_on_error: bool = False
    ) -> list[PushNotification]:
        return await self._command(
            "ai",
            command="getLastPUSHNotifications",
            expected_type=list,
            value_on_err=(lambda: []) if default_on_error else None,
        )

    async def list_categories(self) -> list[str]:
        return await self._command(
            "hh",
            command="getCategories",
            expected_type=list,
            # the API sometimes wrongly returns an empty list, but there are also appliances (ex. AdoraWash V4000) which don't have any categories
            reject_empty=False,
        )

    async def get_category(self, value: str) -> Category:
        return await self._command(
            "hh", command="getCategory", params={"value": value}, expected_type=dict
        )

    async def list_commands(self, value: str) -> list[str]:
        return await self._command(
            "hh", command="getCommands", params={"value": value}, expected_type=list
        )

    async def get_command(self, value: str) -> Command:
        return await self._command(
            "hh", command="getCommand", params={"value": value}, expected_type=dict
        )

    async def set_command(self, command: str, value: str) -> None:
        await self._command(
            "hh",
            command=f"set{command}",
            params={"value": value},
            raw=True,
            attempts=2,
        )

    async def do_command_action(self, command: str) -> None:
        await self._command(
            "hh",
            command=f"do{command}",
            raw=True,
            attempts=2,
        )

    async def get_hh_fw_version(self, *, default_on_error: bool = False) -> HhFwVersion:
        return await self._command(
            "hh",
            command="getFWVersion",
            expected_type=dict,
            value_on_err=(lambda: HhFwVersion()) if default_on_error else None,
        )

    async def get_ai_fw_version(self, *, default_on_error: bool = False) -> AiFwVersion:
        return await self._command(
            "ai",
            command="getFWVersion",
            expected_type=dict,
            value_on_err=(lambda: AiFwVersion()) if default_on_error else None,
        )

    async def get_zh_mode(self, *, default_on_error: bool = False) -> int:
        data = await self._command(
            "hh",
            command="getZHMode",
            expected_type=dict,
            value_on_err=(lambda: {"value": -1}) if default_on_error else None,
        )
        return data["value"]

    async def get_eco_info(self, *, default_on_error: bool = False) -> EcoInfo:
        return await self._command(
            "hh",
            command="getEcoInfo",
            expected_type=dict,
            value_on_err=(lambda: EcoInfo()) if default_on_error else None,
        )

    async def get_device_info(self) -> DeviceInfo:
        # TODO: use this to replace a part of aggregations, display api version as a diagnostic sensor
        # 'getAPIVersion' can be used to get only the API version
        # 'getZHMode' gives just the zh mode
        return await self._command(
            "hh",
            command="getDeviceInfo",
            expected_type=dict,
        )

    async def get_program(self) -> list[Program]:
        # TODO: this is interesting but what can we do with it??
        # [{"id":52,"name":"Alltag Kurz","status":"selected","starttime":{"min":0,"max":86400,"step":600},"duration":{"set":2460}, "energySaving":{"set":false,"options":[true,false]},"optiStart":{"set":false},"steamfinish":{"set":false,"options":[true,false]},"partialload":{"set":false,"options":[true,false]},"rinsePlus":{"set":false,"options":[true,false]},"dryPlus":{"set":false,"options":[true,false]},"stepIds":[82,81,82,79,78,76,73,74,75,72,71,70]}]
        # [{"id":50,"name":"Eco",        "status":"selected","starttime":{"min":0,"max":86400,"step":600},"duration":{"set":22440},"energySaving":{"set":false,"options":[true,false]},"optiStart":{"set":false},"steamfinish":{"set":true, "options":[true,false]},"partialload":{"set":false,"options":[true,false]},"rinsePlus":{"set":false,"options":[true,false]},"dryPlus":{"set":false,"options":[true,false]},"stepIds":[79,81,79,78,74,75,72,70]}]
        raw_programs: list[dict[str, Any]] = await self._command(
            "hh",
            command="getProgram",
            expected_type=list,
        )
        return [Program.build(raw) for raw in raw_programs]

    async def set_program(
        self, program_id: int, options: dict[str, Any] | None = None
    ) -> list[Any]:
        # example options: {"id":50,"dryPlus":false,"energySaving":false,"partialload":false,"rinsePlus":false,"steamfinish":true}
        # also seen with just the "id" key
        if not options:
            options = {}
        options["id"] = program_id
        return await self._command(
            "hh",
            command="setProgram",
            params={"value": json.dumps(options)},
            raw=True,
            attempts=2,
        )

    async def get_all_program_ids(self) -> list[int]:
        # TODO: this gives us a nice list of ids that could be used with set_program, but we need a program id to name mapping
        return await self._command(
            "hh",
            command="getAllProgramIds",
            expected_type=list,
        )


# TODO: DISCOVER devices:
#   broadcast b"DISCOVERY_LAN_INTERFACE_REQUEST" to 2047
#   response: b"DISCOVERY_LAN_INTERFACE_RESPONSE" on port 2047

# TODO: discover with dhcp: fc:1b:ff