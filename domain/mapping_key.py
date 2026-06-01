
VoltageKv = Union[int, float, str]


@dataclass(frozen=True)
class MappingKey:
    """Canonical key used to join IPS devices to PowerFactory elements."""
    site_code: str
    voltage_kv: VoltageKv
    designation: str