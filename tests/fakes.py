"""Minimal PowerFactory stand-ins for offline tests of pure helpers."""

from typing import Any, Dict, List, Optional


class FakePF:
    """A DataObject-like object: attributes via GetAttribute/SetAttribute."""

    def __init__(self, loc_name: str = "obj", cls: str = "ElmRelay",
                 attrs: Optional[Dict[str, Any]] = None,
                 contents: Optional[List["FakePF"]] = None,
                 parent: Optional["FakePF"] = None):
        self.loc_name = loc_name
        self._cls = cls
        self.attrs: Dict[str, Any] = dict(attrs or {})
        self.contents: List[FakePF] = list(contents or [])
        self.parent = parent
        self.writes: List[tuple] = []
        for child in self.contents:
            child.parent = self

    # --- DataObject API used by the code under test --------------------
    def GetClassName(self) -> str:
        return self._cls

    def GetAttribute(self, name: str) -> Any:
        key = name[2:] if name.startswith("e:") else name
        if key not in self.attrs:
            raise AttributeError(name)
        return self.attrs[key]

    def SetAttribute(self, name: str, value: Any) -> None:
        key = name[2:] if name.startswith("e:") else name
        current = self.attrs.get(key)
        if isinstance(current, (int, float)) and not isinstance(current, bool) \
                and isinstance(value, str):
            raise TypeError(f"{name} expects a number")
        self.attrs[key] = value
        self.writes.append((key, value))

    def GetContents(self, pattern: str = "*", recursive: bool = False):
        found = []
        for child in self.contents:
            if _match(pattern, child):
                found.append(child)
            if recursive:
                found.extend(child.GetContents(pattern, True))
        return found

    def GetParent(self):
        return self.parent

    @property
    def fold_id(self):
        return self.parent

    def GetFullName(self) -> str:
        chain = []
        node = self
        while node is not None:
            chain.append(f"{node.loc_name}.{node._cls}")
            node = node.parent
        return "\\".join(reversed(chain))

    def __repr__(self) -> str:
        return f"<FakePF {self.loc_name}.{self._cls}>"


def _match(pattern: str, obj: FakePF) -> bool:
    if pattern in ("*", "*.*"):
        return True
    if pattern.startswith("*."):
        return obj.GetClassName() == pattern[2:]
    if "." in pattern:
        name, cls = pattern.rsplit(".", 1)
        return obj.loc_name == name and obj.GetClassName() == cls
    return obj.loc_name == pattern


class FakeDevice:
    """ProtectionDevice-shaped object without the PF/IPS plumbing."""

    def __init__(self, device: str, name: str, pf_obj: Any = None,
                 settings: Optional[list] = None, **extra):
        self.device = device
        self.name = name
        self.pf_obj = pf_obj
        self.settings = settings if settings is not None else [["b", "p", "1", ""]]
        self.setting_id = extra.pop("setting_id", "SID")
        self.date = extra.pop("date", "2026-01-01")
        self.ct_primary = extra.pop("ct_primary", 400)
        self.ct_secondary = extra.pop("ct_secondary", 5)
        for key, value in extra.items():
            setattr(self, key, value)
