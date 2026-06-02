import re
from process_pf_elements import (
    bus_parser as bp,
    line_parser as lp,
    cap_bank_parser as cbp,
    switch_parser as swp,
    tfmr_parser as tp,
    tfmr_names as tn)
from domain import sub_dataclass as dc, inspect_dataclass as ind

from importlib import reload
reload(dc)
reload(bp)
reload(ind)

_STRUCTURED_SWITCH = re.compile(r"^(CB|AB|IS)")

def process_elements(app, selected_grid):

    selected_grid = selected_grid.GetContents()

    lines = [element for element in selected_grid
                 if element.GetClassName() == 'ElmLne']
    busbars = [element for element in selected_grid
                 if element.GetClassName() == 'ElmTerm']
    switches = [element for element in selected_grid
                 if element.GetClassName() == 'ElmCoup']
    cap_banks = [element for element in selected_grid
                 if element.GetClassName() == 'ElmShnt']
    tr_2winds = [element for element in selected_grid
                 if element.GetClassName() == 'ElmTr2']
    tr_3winds = [element for element in selected_grid
                 if element.GetClassName() == 'ElmTr3']

    sites = []
    failed_matches = dc.FailedMatches([], [], [])

    app.PrintPlain("Parsing busbars...")
    for bus in busbars:

        # Get voltage level
        nominal_kv =bus.GetAttribute("uknom")

        # Get name
        parsed_bus = bp.parse_bus(bus.loc_name)
        if parsed_bus is not None:
            bus_name = parsed_bus.name

            # Build element
            bus_element = dc.Element(
                name=bus_name,
                obj=bus,
                element_type=dc.ElementType.BUSBAR,
                relay_cubicle=dc.RelayCubicle(None, None)
            )

            # Assign element to a site
            site_names = {site.name: site for site in sites}
            site = site_names.get(parsed_bus.substation)
            if site is None:
                site = dc.Site(parsed_bus.substation)
                sites.append(site)
            add_element(site, nominal_kv, bus_element)

    app.PrintPlain("Parsing lines...")
    for line in lines:
        # Get name
        feeder_name = lp.extract_leading_number(line.loc_name)
        if feeder_name is None:
            feeder_name = line.loc_name

        cubicle_i = line.bus1
        if cubicle_i is not None:
            terminal_i = cubicle_i.cterm
            result_i = add_element_by_obj_match(sites, terminal_i)
        else: result_i = None
        cubicle_j = line.bus2
        if cubicle_j is not None:
            terminal_j = cubicle_j.cterm
            result_j = add_element_by_obj_match(sites, terminal_j)
        else: result_j = None

        # If both terminals match buses belonging to a single substation, this line is a bus coupler.
        if result_i is not None and result_j is not None and result_j[0] == result_i[0]:
            switch_name = swp.strip_trailing_number(line.loc_name)
            cub_i_contents = cubicle_i.GetContents()
            if any(e in cub_i_contents for e in cub_i_contents if e.GetClassName() == 'StaSwitch'):
                cubicle = cubicle_i
            else:
                cubicle = cubicle_j
            new_element = dc.Element(
                name=switch_name,
                obj=line,
                element_type=dc.ElementType.SWITCH,
                relay_cubicle=dc.RelayCubicle(cubicle, None)
            )
            result_i[1].add(new_element)
        # If a terminal matches any bus belonging to a substation,
        # add the line to that substation as a feeder element.
        else:
            if result_i is not None:
                new_element = dc.Element(
                    name=feeder_name,
                    obj=line,
                    element_type=dc.ElementType.FEEDER,
                    relay_cubicle=dc.RelayCubicle(cubicle_i, None)
                )
                result_i[1].add(new_element)
            if result_j is not None:
                new_element = dc.Element(
                    name=feeder_name,
                    obj=line,
                    element_type=dc.ElementType.FEEDER,
                    relay_cubicle=dc.RelayCubicle(cubicle_j, None)
                )
                result_j[1].add(new_element)

    app.PrintPlain("Parsing cap banks...")
    for cap_bank in cap_banks:

        # Get voltage
        nominal_kv = cap_bank.ushnm

        # Get name
        parsed_cap_bank = cbp.parse_cap_bank(cap_bank.loc_name)
        if parsed_cap_bank is None:
            cap_bank_name = cap_bank.loc_name
        else:
            cap_bank_name = parsed_cap_bank.name

        # Build element
        cap_bank_element = dc.Element(
            name=cap_bank_name,
            obj=cap_bank,
            element_type=dc.ElementType.CAPACITOR_BANK,
            relay_cubicle=dc.RelayCubicle(cap_bank.bus1, None)
        )

        # Assign element to a site
        if parsed_cap_bank is not None:
            new_site = parsed_cap_bank.substation
            site = check_new_site(sites, new_site)
            add_element(site, nominal_kv, cap_bank_element)
        else:
            result = add_element_by_obj_match(sites, cap_bank.bus1.cterm)
            if result is not None:
                result[1].add(cap_bank_element)
            else:
                failed_matches.cap_banks.append(cap_bank)

    app.PrintPlain("Parsing switches...")
    for switch in switches:

        # ---- Voltage + cubicle from the connected terminal ----------------
        cub_1 = switch.bus1
        cub_2 = switch.bus2
        if cub_1 is not None:
            nominal_kv = cub_1.cterm.uknom  # was cub_1.cterm (a terminal object)
            cubicle = cub_1
        elif cub_2 is not None:
            nominal_kv = cub_2.cterm.uknom
            cubicle = cub_2
        else:
            nominal_kv = None
            cubicle = None

        # ---- Raw operating designation ------------------------------------
        parsed_switch = swp.parse_switch(switch.loc_name)
        s_raw_name = parsed_switch.name if parsed_switch is not None else switch.loc_name

        # Strip a trailing "/<n>" duplicate/variant marker before decoding
        # (CB4452/1 -> CB4452, CB1X11/12 -> CB1X11).
        s_raw_name = s_raw_name.split("/")[0]

        # Only structured CB/AB/IS designations decode positionally; generic
        # ("Breaker/Switch") or short ("MTC") names can't be keyed.
        if len(s_raw_name) < 4 or not swp.is_structured_switch(s_raw_name):
            failed_matches.switches.append(switch)
            continue

        # ---- Decode element type + operating name -------------------------
        if s_raw_name[3] == "C":
            name = "CP" + s_raw_name[5:6]
            el_type = dc.ElementType.CAPACITOR_BANK
        elif s_raw_name[3] == "K":
            name = s_raw_name
            el_type = dc.ElementType.GEN_CUBICLE
        elif s_raw_name[3] == "T":
            # Transformer breaker -> TR<n> later via tn.update_element_names.
            # Only CB<v>T<n> codes are decodable; others (e.g. IS3T29) are dropped.
            if not swp.is_transformer_switch_code(s_raw_name):
                failed_matches.switches.append(switch)
                continue
            name = s_raw_name
            el_type = dc.ElementType.TRANSFORMER
        elif "Spare" in s_raw_name or "SPARE" in s_raw_name:
            name = s_raw_name
            el_type = dc.ElementType.SPARE_SWITCH
        elif s_raw_name[3] == "X" or s_raw_name[2] == "1":
            # (d)=X bus coupler, or any 11 kV ((c)=1) switch -> 11 kV bus coupler.
            name = s_raw_name
            el_type = dc.ElementType.SWITCH
        else:
            # Bus coupler (both terminals on one substation) or a feeder.
            terminal_i = cub_1.cterm if cub_1 is not None else None
            terminal_j = cub_2.cterm if cub_2 is not None else None
            result_i = add_element_by_obj_match(sites, terminal_i)
            result_j = add_element_by_obj_match(sites, terminal_j)

            if result_i is not None and result_j is not None and result_j[0] == result_i[0]:
                name = s_raw_name
                el_type = dc.ElementType.SWITCH
            elif s_raw_name[2] in ("3", "4", "5", "6", "7", "8"):
                core = s_raw_name[2:]
                if core.endswith("2"):  # trailing (f) constant -> strip
                    core = core[:-1]
                if not core.isdigit():  # a feeder number is numeric
                    failed_matches.switches.append(switch)
                    continue
                name = "F" + core
                el_type = dc.ElementType.FEEDER
            else:
                failed_matches.switches.append(switch)
                continue

        # ---- Build + assign to a site -------------------------------------
        new_element = dc.Element(
            name=name,
            obj=switch,
            element_type=el_type,
            relay_cubicle=dc.RelayCubicle(cubicle, None),
        )

        if parsed_switch is not None:
            site = check_new_site(sites, parsed_switch.substation)
            add_element(site, nominal_kv, new_element)
        else:
            cub = switch.bus1
            if cub is not None:
                result = add_element_by_obj_match(sites, cub.cterm)
                if result is not None:
                    result[1].add(new_element)
                else:
                    failed_matches.switches.append(switch)
            else:
                failed_matches.switches.append(switch)

    app.PrintPlain("Parsing transformers...")
    for tr_2w in tr_2winds:

        # Get voltage levels
        tr_type = tr_2w.typ_id
        if tr_type is not None:
            nominal_hv_kv = tr_2w.typ_id.utrn_h
            nominal_lv_kv = tr_2w.typ_id.utrn_l

        # Get name
        parsed_tfmr = tp.parse_tfmr(tr_2w.loc_name)
        if parsed_tfmr:
            tfmr_name = parsed_tfmr.name
        else:
            tfmr_name = tr_2w.loc_name

        # Build elements
        tfmr_hv_element = dc.Element(
            name=tfmr_name,
            obj=tr_2w,
            element_type=dc.ElementType.TRANSFORMER_HV,
            relay_cubicle=dc.RelayCubicle(tr_2w.bushv, None)
        )
        tfmr_lv_element = dc.Element(
            name=tfmr_name,
            obj=tr_2w,
            element_type=dc.ElementType.TRANSFORMER_LV,
            relay_cubicle=dc.RelayCubicle(tr_2w.buslv, None)
        )

        # Assign element to a site
        if parsed_tfmr is not None and tr_type is not None:
            new_site = parsed_tfmr.substation
            site = check_new_site(sites, new_site)
            add_element(site, nominal_hv_kv, tfmr_hv_element)
            add_element(site, nominal_lv_kv, tfmr_lv_element)
        else:
            result_hv = add_element_by_obj_match(sites, tr_2w.bushv.cterm)
            if result_hv is not None:
                result_hv[1].add(tfmr_hv_element)
            else:
                failed_matches.tfmrs.append(tr_2w)
            result_lv = add_element_by_obj_match(sites, tr_2w.buslv.cterm)
            if result_lv is not None:
                result_lv[1].add(tfmr_lv_element)
            else:
                if tr_2w not in failed_matches.tfmrs:
                    failed_matches.tfmrs.append(tr_2w)

    for tr_3w in tr_3winds:

        # Get voltage levels
        tr_type = tr_3w.typ_id
        if tr_type is not None:
            nominal_hv_kv = tr_3w.typ_id.utrn3_h
            nominal_mv_kv = tr_3w.typ_id.utrn3_m
            nominal_lv_kv = tr_3w.typ_id.utrn3_l

        # Get name
        parsed_tfmr = tp.parse_tfmr(tr_3w.loc_name)
        if parsed_tfmr:
            tfmr_name = parsed_tfmr.name
        else:
            tfmr_name = tr_3w.loc_name

        # Build elements
        tfmr_hv_element = dc.Element(
            name=tfmr_name,
            obj=tr_3w,
            element_type=dc.ElementType.TRANSFORMER_HV,
            relay_cubicle=dc.RelayCubicle(tr_3w.bushv, None)
        )
        tfmr_mv_element = dc.Element(
            name=tfmr_name,
            obj=tr_3w,
            element_type=dc.ElementType.TRANSFORMER_LV_B,
            relay_cubicle=dc.RelayCubicle(tr_3w.busmv, None)
        )
        tfmr_lv_element = dc.Element(
            name=tfmr_name,
            obj=tr_3w,
            element_type=dc.ElementType.TRANSFORMER_LV_A,
            relay_cubicle=dc.RelayCubicle(tr_3w.buslv, None)
        )

        # Assign element to a site
        if parsed_tfmr is not None and tr_type is not None:
            new_site = parsed_tfmr.substation
            site = check_new_site(sites, new_site)
            add_element(site, nominal_hv_kv, tfmr_hv_element)
            add_element(site, nominal_mv_kv, tfmr_mv_element)
            add_element(site, nominal_lv_kv, tfmr_lv_element)
        else:
            result_hv = add_element_by_obj_match(sites, tr_3w.bushv.cterm)
            if result_hv is not None:
                result_hv[1].add(tfmr_hv_element)
            else:
                failed_matches.tfmrs.append(tr_3w)
            result_mv = add_element_by_obj_match(sites, tr_3w.busmv.cterm)
            if result_mv is not None:
                result_mv[1].add(tfmr_mv_element)
            else:
                if tr_3w not in failed_matches.tfmrs:
                    failed_matches.tfmrs.append(tr_3w)
            result_lv = add_element_by_obj_match(sites, tr_3w.buslv.cterm)
            if result_lv is not None:
                result_lv[1].add(tfmr_lv_element)
            else:
                if tr_3w not in failed_matches.tfmrs:
                    failed_matches.tfmrs.append(tr_3w)

    # Update bus cubicle for every bus at every site
    for site in sites:
        busbars = [el for v1 in site.voltage_levels.values()
                   for el in v1.elements.get(dc.ElementType.BUSBAR, {}).values()]
        for bus in busbars:
            cub = bp.determine_bus_cubicle(bus.obj, site)
            bus.relay_cubicle = dc.RelayCubicle(cub, None)

    # Update elmcoup transformer names
    for site in sites:
        tfmrs = [el for v1 in site.voltage_levels.values()
                   for el in v1.elements.get(dc.ElementType.TRANSFORMER, {}).values()]
        tn.update_element_names(tfmrs)

    return sites


def check_new_site(sites,new_site):
    site_names = {site.name: site for site in sites}
    site = site_names.get(new_site)
    if site is None:
        site = dc.Site(new_site)
        sites.append(site)
    return site


def add_element(substation: dc.Site, nominal_kv: float, element: dc.Element) -> None:
    v1 = substation.add_voltage_level(nominal_kv)
    v1.add(element)


def _iter_elements(substation: dc.Site):
    """Yield (voltage_level, element) for every element in the substation."""
    for vl in substation.voltage_levels.values():
        for by_name in vl.elements.values():
            for el in by_name.values():
                yield vl, el


def add_element_by_obj_match(
    substations: list[dc.Site],
    obj_match: object,
    key=lambda o: o,
) -> tuple[dc.Site, dc.VoltageLevel] | None:
    """Add `element` to the substation/voltage level that already contains an
    element named obj_match. Returns (substation, voltage_level) on success,
    or None if no match was found (element not added)."""
    if obj_match is None:
        return None
    target = key(obj_match)

    for substation in substations:
        for vl, existing in _iter_elements(substation):
            if existing.obj is not None and key(existing.obj) == target:
                return substation, vl
    return None
