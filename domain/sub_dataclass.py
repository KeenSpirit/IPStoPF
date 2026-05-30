from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, List, Optional, TYPE_CHECKING
from enum import Enum
from collections import defaultdict

if TYPE_CHECKING:
    from pf_config import pft


class ElementType(Enum):
    TRANSFORMER_HV   = "Transformer HV"
    TRANSFORMER_LV   = "Transformer LV"
    TRANSFORMER_LV_A = "Transformer LV A"
    TRANSFORMER_LV_B = "Transformer LV B"
    BUSBAR           = "Busbar"
    SWITCH           = "Switch"
    CAPACITOR_BANK   = "Capacitor bank"
    FEEDER           = "Feeder"


@dataclass
class RelayCubicle:
    obj: object | None
    relay_model: str | None = None          # e.g. "MiCOM P543"
    # extend later: settings group, CT/VT ratios, comms address, IPS ref...


@dataclass
class Element:
    name: str
    obj: object
    element_type: ElementType
    relay_cubicle: Any[None | RelayCubicle]


@dataclass
class VoltageLevel:
    nominal_kv: float | None
    # element_type -> { element_name -> Element }
    elements: dict[ElementType, dict[str, Element]] = field(
        default_factory=lambda: defaultdict(dict)
    )

    def add(self, element: Element) -> Element:
        self.elements[element.element_type][element.name] = element
        return element

    def get(self, element_type: ElementType, name: str) -> Element | None:
        return self.elements.get(element_type, {}).get(name)

    def of_type(self, element_type: ElementType) -> list[Element]:
        return list(self.elements.get(element_type, {}).values())


@dataclass
class Site:
    name: str
    voltage_levels: dict[float | str, VoltageLevel] = field(default_factory=dict)

    def add_voltage_level(self, nominal_kv: float) -> VoltageLevel:
        return self.voltage_levels.setdefault(nominal_kv, VoltageLevel(nominal_kv))

    def get_voltage_level(self, nominal_kv: float) -> VoltageLevel | None:
        return self.voltage_levels.get(nominal_kv)

# ss = Site("Springfield")
#
# hv = ss.add_voltage_level(110.0)
# mv = ss.add_voltage_level(11.0)
#
# hv.add(Element("T1 HV", ElementType.TRANSFORMER_HV, RelayCubicle("T1-HV-CUB", "MiCOM P643")))
# mv.add(Element("T1 LV A", ElementType.TRANSFORMER_LV_A, RelayCubicle("T1-LVA-CUB")))
# mv.add(Element("Bus A",   ElementType.BUSBAR,          RelayCubicle("BUSA-CUB")))
# mv.add(Element("Feeder 1", ElementType.FEEDER, RelayCubicle("FDR1-CUB", "MiCOM P143")))
# mv.add(Element("Feeder 2", ElementType.FEEDER, RelayCubicle("FDR2-CUB")))
#
# # specific element -> O(1)
# f1 = ss.get_voltage_level(11.0).get(ElementType.FEEDER, "Feeder 1")
# print(f1.relay_cubicle.relay_model)        # MiCOM P143
#
# # all feeders at 11 kV
# for fdr in ss.get_voltage_level(11.0).of_type(ElementType.FEEDER):
#     print(fdr.name, fdr.relay_cubicle.name)


@dataclass
class FailedMatches:
    cap_banks: list["pft.ElmShnt"]
    switches: list["pft.ElmCoup"]
    tfmrs: list[Any]

