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


"""
Regional - Capricorna
Regional - Far North
Regional - Mackay
Regional - North Queensland
Regional - South West
Regional - Wide Bay
"""


def regional_grid(app, acronym: str) ->list:
    """

    Args:
        app:
        acronym: CA, FN, MK, NQ, SW, WB

    Returns:

    """

    try:
        ee_obj_fold = app.GetProjectFolder("netdat").GetContents("EnergyQld subtransmission")[0]
    except AttributeError:
        app.PrintPlain("Could not locate EnergyQld subtransmission folder. Check the TransSubtrans project is Active.")
        sys.exit(0)
    ee_grid_fold =  ee_obj_fold.GetContents()

    regional_grids = ([grid for grid in ee_grid_fold
                 if grid.GetClassName() == 'ElmNet'
                  and [element for element in grid.GetContents() if element.GetClassName() == 'ElmTerm']
                  and grid.IsCalcRelevant()
                  and grid.loc_name[:1] == acronym]
                 )

    regional_grids_sorted = sorted(regional_grids, key=lambda grid: grid.GetAttribute('loc_name'))

    return regional_grids_sorted