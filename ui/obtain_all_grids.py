import sys

def all_egx_grids(app):
    """Returns a list of active grids within the Energex network"""

    try:
        egx_obj_fold = app.GetProjectFolder("netdat").GetContents("Energex_Master")[0]
    except AttributeError:
        app.PrintPlain("Could not locate Energex_Master folder. Check the TransSubtrans project is Active.")
        sys.exit(0)
    egx_grid_fold = egx_obj_fold.GetContents()
    egx_grids = ([grid for grid in egx_grid_fold
                 if grid.GetClassName() == 'ElmNet'
                  and [element for element in grid.GetContents() if element.GetClassName() == 'ElmTerm']
                  and grid.IsCalcRelevant()]
                 )

    exg_grids_sorted = sorted(egx_grids, key=lambda grid: grid.GetAttribute('loc_name'))

    return exg_grids_sorted


def all_ca_grids(app):
    pass

    try:
        ee_obj_fold = app.GetProjectFolder("netdat").GetContents("Energex_Master")[0]
    except AttributeError:
        app.PrintPlain("Could not locate Energex_Master folder. Check the TransSubtrans project is Active.")
        sys.exit(0)


def all_fn_grids(app):
    pass

def all_mk_grids(app):
    pass

def all_nq_grids(app):
    pass

def all_sw_grids(app):
    pass