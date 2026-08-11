# SPDX-FileCopyrightText: Open Energy Transition gGmbH
#
# SPDX-License-Identifier: MIT

"""
Functions used by `analysis.ipynb` to post-process solved PyPSA-Eur networks, in the
context of the noon-energy-project.

The notebook holds everything that is meant to be changed - which results to read,
which carriers to show, colours, hatches and periods - while this module holds the
calculations and the plots, which stay the same across the scenarios studied.

Contents
--------
Read results
    load_networks             solved networks of a run, one per scenario;
                              the run differs in terms of spatial clustering (e.g.,
                              bidding zones vs 52 buses); the scenarios studied
                              in each run are defined in ../config/scenarios.noon.yaml
Process results
    get_storage_capacity      energy and power capacity of each storage technology
    get_electricity_mix       electricity generated and installed capacity
    get_electricity_balance   electricity balance of a country over time
    get_kpis                  system cost, res storage investments, curtailment, prices
    summary_by_carrier        totals per carrier, for Europe or for one country
Tables comparing scenarios
    summary_table             summary_by_carrier of every scenario, side by side
    kpi_table                 get_kpis of every scenario, side by side
Plots
    plot_map                  map of Europe with one pie per bus
    plot_balance              electricity balance over one or more periods

Every function takes a solved network `n` (or a dict of them) and returns plain
pandas objects, so results can also be inspected or exported from the notebook.
"""

import io
import math
import os
from pathlib import Path

import cartopy.crs as ccrs
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import pypsa
from matplotlib.collections import PatchCollection
from matplotlib.lines import Line2D
from matplotlib.patches import Circle, Patch, PathPatch, Rectangle
from pypsa.plot.maps.static import add_legend_circles

# Column names used across the tables and the plots. The unit in brackets is read
# back by plot_map to label the legend, so keep the "<quantity> (<unit>)" shape.
ENERGY_CAPACITY = "Energy capacity (GWh)"
POWER_CAPACITY = "Power capacity (GW)"
ELECTRICITY_MIX = "Electricity mix (TWh)"

# The electricity of a PyPSA-Eur network flows on two bus carriers: the transmission
# grid ("AC") and the distribution grid ("low voltage"), where rooftop solar and home
# batteries sit. Both are always read together and reported on the AC bus.
ELECTRICITY_BUSES = ["AC", "low voltage"]


# ---------------------------------------------------------------------------
# Read results
# ---------------------------------------------------------------------------


def load_networks(run, scenarios, results_dir="../results", clusters="52"):
    """
    Load the solved network of every scenario of a run, as {scenario: network}.

    Each file is expected at the location Snakemake writes it to:

        <results_dir>/<run>/<scenario>/networks/base_s_<clusters>___<year>.nc

    where <year> is the planning year, i.e. the last part of the scenario name
    ("cy2021-lds-2040" -> 2040). Scenarios that have not been solved are reported
    and skipped, so the same list of scenarios can be kept while runs are ongoing.

    Every network is named "<scenario> | <run>", which is what titles and file names
    of the figures are built from, so that results of different runs never overwrite
    each other.
    """
    networks = {}

    for scenario in scenarios:
        year = scenario.rsplit("-", 1)[-1]
        path = (
            Path(results_dir)
            / run
            / scenario
            / "networks"
            / f"base_s_{clusters}___{year}.nc"
        )

        if not path.exists():
            print(f"missing: {scenario:<30} ({path})")
            continue

        n = pypsa.Network(path)
        n.name = f"{scenario} | {run}"
        networks[scenario] = n
        print(f"loaded:  {scenario}")

    if not networks:
        raise FileNotFoundError(
            f"no solved network found for run '{run}'. Networks are looked up as "
            f"{results_dir}/{run}/<scenario>/networks/base_s_{clusters}___<year>.nc - check "
            f"the name of the run, the number of clusters ('{clusters}': the runs on "
            f"bidding zones use 'adm') and the names of the scenarios."
        )

    return networks


# ---------------------------------------------------------------------------
# Process results
# ---------------------------------------------------------------------------


def get_storage_capacity(n, technologies, non_electric_stores=()):
    """
    Energy (GWh) and power (GW) capacity of each storage technology, per bus.

    `technologies` maps the carrier used in the network to the name to show in plots
    and tables, e.g. {"lfp": "lfp 6h", "PHS": "phs", "res": "res"}. Carriers that are
    not part of a given network are ignored, so one list works for every scenario.

    PyPSA-Eur models storage in two ways and this function combines both:

    * as a StorageUnit - a single component holding energy and power, whose energy
      capacity is the power capacity times the fixed duration of the technology
      (max_hours in the config settings). Used for li-ion, pair, mds, PHS and for
      `res` when it is given a fixed duration (res 24h, res 100h, ...).
    * as a Store plus a pair of charger/discharger Links - the Store holds the energy
      and the Links the power. Used for home batteries and for `res` in the "store"
      scenarios, where the duration is optimised instead of being fixed.

    Everything is reported in electricity, so that all the technologies can be compared
    with each other whichever way they are modelled:

    * energy is the capacity of the component, which already holds electricity - except
      for the carriers listed in `non_electric_stores`, whose store holds something
      else. A "res" store holds a carbon medium, of which only the efficiency of the
      discharger comes back out as electricity, while a home battery stores electricity
      directly and is reported as it is.
    * power is what the technology can put back into the grid. The rating of a
      discharger link is given at its input, on the storage side, so it is multiplied
      by the efficiency of the link; the rating of a storage unit is already the one
      seen from the grid.
    """
    tables = []

    # 1) Technologies modelled as StorageUnits
    units = n.storage_units[n.storage_units.carrier.isin(technologies)]
    if not units.empty:
        power = units.p_nom_opt / 1e3  # MW -> GW
        tables.append(
            pd.DataFrame(
                {
                    "bus": units.bus,
                    "carrier": units.carrier,
                    ENERGY_CAPACITY: power * units.max_hours,
                    POWER_CAPACITY: power,
                }
            )
        )

    # 2) Technologies modelled as a Store plus charger/discharger Links
    for carrier in technologies:
        stores = n.stores[n.stores.carrier == carrier]
        dischargers = n.links[n.links.carrier == f"{carrier} discharger"]
        if stores.empty or dischargers.empty:
            continue

        # Store and discharger are named after the electricity bus they belong to:
        # "DE0 0 home battery" and "DE0 0 low voltage" both belong to bus "DE0 0".
        store_bus = stores.bus.str.replace(f" {carrier}", "", regex=False)
        grid_bus = dischargers.bus1.str.replace(" low voltage", "", regex=False)

        energy = (stores.e_nom_opt / 1e3).groupby(store_bus).sum()
        if carrier in non_electric_stores:
            energy = energy * dischargers.efficiency.groupby(grid_bus).mean()

        tables.append(
            pd.DataFrame(
                {
                    "carrier": carrier,
                    ENERGY_CAPACITY: energy,
                    POWER_CAPACITY: (
                        dischargers.p_nom_opt * dischargers.efficiency / 1e3
                    )
                    .groupby(grid_bus)
                    .sum(),
                }
            )
            .rename_axis("bus")
            .reset_index()
        )

    if not tables:
        return pd.DataFrame(
            columns=[ENERGY_CAPACITY, POWER_CAPACITY],
            index=pd.MultiIndex.from_arrays([[], []], names=["bus", "carrier"]),
        )

    capacity = pd.concat(tables, ignore_index=True)
    capacity["carrier"] = capacity["carrier"].map(technologies)

    return capacity.groupby(["bus", "carrier"]).sum().round(2)


def get_electricity_mix(n, technologies):
    """
    Electricity generated (TWh) and installed capacity (GW) per bus and carrier.

    `technologies` maps the carriers of the network to the groups to show, e.g.
    {"CCGT": "fossil fuels", "OCGT": "fossil fuels", "onwind": "onwind", ...}. Only
    the carriers listed there are kept, so anything that is not electricity
    generation (storage, conversion to other energy vectors) is left out.

    "AC" and "DC" are the transmission grid seen from a bus, i.e. its net import;
    they are part of the mix but have no capacity of their own, so they only appear
    in the generation column.
    """
    generation = (
        n.statistics.energy_balance(
            bus_carrier=ELECTRICITY_BUSES,
            nice_names=False,
            groupby=["bus", "carrier"],
        ).droplevel(0)
        / 1e6
    )  # MWh -> TWh
    generation = generation[generation.index.get_level_values(1).isin(technologies)]

    capacity = (
        n.statistics.optimal_capacity(
            bus_carrier=ELECTRICITY_BUSES,
            nice_names=False,
            groupby=["bus", "carrier"],
        ).droplevel(0)
        / 1e3
    )  # MW -> GW
    transmission = ["AC", "DC"]
    capacity = capacity[
        capacity.index.get_level_values(1).isin(
            [c for c in technologies if c not in transmission]
        )
    ]

    mix = pd.concat(
        [generation.rename(ELECTRICITY_MIX), capacity.rename(POWER_CAPACITY)], axis=1
    )

    # Rooftop solar and home batteries sit on the distribution bus of a region:
    # report them on the region's transmission bus, together with everything else.
    mix = mix.rename(index=lambda bus: bus.replace(" low voltage", ""), level=0)
    mix = mix.rename(index=technologies, level=1)

    return mix.groupby(["bus", "carrier"]).sum().fillna(0).round(2)


def get_electricity_balance(n, country, carrier_labels):
    """
    Electricity balance (GW) of a country, per carrier and time step.

    Positive values feed the grid (generation, storage discharge, imports), negative
    values withdraw from it (demand, storage charge, exports). `carrier_labels`
    renames the carriers of the network into the names used in the plot; carriers
    mapped to the same name are summed together.
    """
    balance = (
        n.statistics.energy_balance(
            bus_carrier=ELECTRICITY_BUSES,
            nice_names=False,
            groupby=["country", "carrier"],
            groupby_time=False,
        )
        .droplevel(0)
        .fillna(0)
        / 1e3
    )  # MW -> GW

    balance = balance.loc[country]

    return balance.rename(index=carrier_labels).groupby("carrier").sum()


def get_kpis(
    n, generation_carriers, investment_carriers, renewable_carriers, country=None
):
    """
    System indicators of a scenario, for Europe and optionally for one country.

    Returns a DataFrame with one column per region and four rows:

    * system cost - total annualised cost of the electricity system (b EUR/a),
      excluding the cost of unserved load,
    * storage investments - annualised capital cost of the newly built capacity of
      `investment_carriers` (b EUR/a),
    * curtailment - electricity spilled by `renewable_carriers` as a share of what the
      system produces in total, that is

          curtailed(renewables) / [ curtailed(renewables) + generated(all technologies) ]

      where the denominator is the electricity generated by `generation_carriers`, not
      only by the renewable ones: the indicator answers "how much of what the system
      could have produced is thrown away".
    * electricity price - average marginal price of the AC buses (EUR/MWh).
    """
    regions = {"Europe": None}
    if country is not None:
        regions[country] = country

    return pd.DataFrame(
        {
            region: _kpis_for_region(
                n, generation_carriers, investment_carriers, renewable_carriers, cc
            )
            for region, cc in regions.items()
        }
    )


def _kpis_for_region(
    n, generation_carriers, investment_carriers, renewable_carriers, country
):
    """The four indicators of get_kpis for a single region (None = whole system)."""

    def by_country(statistic, **kwargs):
        result = statistic(
            nice_names=False, groupby=["country", "carrier"], **kwargs
        ).droplevel(0)
        return result.loc[country] if country else result

    system_cost = (
        by_country(n.statistics.system_cost, bus_carrier=ELECTRICITY_BUSES) / 1e9
    )
    system_cost = system_cost[system_cost.index.get_level_values("carrier") != "load"]

    investments = by_country(n.statistics.expanded_capex) / 1e9
    investments = investments[
        investments.index.get_level_values("carrier").isin(investment_carriers)
    ]

    # Curtailment of the renewables, against everything the system generates - see the
    # formula in get_kpis. The transmission grid is left out of the denominator: what a
    # bus imports is generated somewhere else, and would be counted twice.
    curtailed = (
        by_country(n.statistics.curtailment, bus_carrier=ELECTRICITY_BUSES) / 1e6
    )
    curtailed = curtailed[
        curtailed.index.get_level_values("carrier").isin(renewable_carriers)
    ]
    generated = (
        by_country(n.statistics.energy_balance, bus_carrier=ELECTRICITY_BUSES) / 1e6
    )
    generated = generated[
        generated.index.get_level_values("carrier").isin(
            [c for c in generation_carriers if c not in ["AC", "DC"]]
        )
    ]

    prices = n.statistics.prices(bus_carrier=["AC"])
    if country:
        prices = prices[prices.index.str.startswith(country)]

    return pd.Series(
        {
            # "Electricity System cost (b€/a)": system_cost.sum(),
            "RES storage investments (b€/a)": investments.sum(),
            "Solar and wind curtailment (%)": 100
            * curtailed.sum()
            / (curtailed.sum() + generated.sum()),
            "Avg. marginal electricity price (€/MWh)": prices.mean(),
        }
    )


def summary_by_carrier(n, df, groups=None, country=None):
    """
    Totals per carrier of a table returned by get_storage_capacity or
    get_electricity_mix, with a total row.

    groups  : optional {carrier: group} to report aggregated technologies, e.g. all
              the durations of a technology on a single row.
    country : two-letter code to restrict the totals to the buses of one country;
              None sums over the whole network.
    """
    if country is not None:
        buses = n.buses.index[(n.buses.carrier == "AC") & (n.buses.country == country)]
        df = df[df.index.get_level_values("bus").isin(buses)]

    table = df.groupby("carrier").sum().round(2)

    if groups:
        table.index = table.index.map(lambda carrier: groups.get(carrier, carrier))
        table = table.groupby(level=0).sum()
        table.index.name = "carrier"

    table.loc["Total"] = table.sum()

    return table


def summary_table(networks, tables, value_col, groups=None, country=None):
    """
    Totals per carrier of every scenario, side by side: one column per scenario.

    networks  : {scenario: network}, as returned by load_networks.
    tables    : {scenario: table} for those same scenarios, as returned by
                get_storage_capacity or get_electricity_mix.
    value_col : column of the tables to report, e.g. ENERGY_CAPACITY.
    groups, country : passed on to summary_by_carrier.

    Any number of scenarios works, a single one included; a carrier missing from a
    scenario is reported as zero there.
    """
    columns = {
        scenario: summary_by_carrier(n, tables[scenario], groups, country)[value_col]
        for scenario, n in networks.items()
        if scenario in tables
    }

    if not columns:
        return pd.DataFrame()

    return pd.concat(columns, axis=1).fillna(0)


def kpi_table(
    networks, generation_carriers, investment_carriers, renewable_carriers, country=None
):
    """
    Indicators of every scenario, side by side: two columns per scenario if a country
    is given, one otherwise. See get_kpis, which is called once per scenario.

    Any number of scenarios works, a single one included.
    """
    columns = {
        scenario: get_kpis(
            n, generation_carriers, investment_carriers, renewable_carriers, country
        )
        for scenario, n in networks.items()
    }

    if not columns:
        return pd.DataFrame()

    return pd.concat(columns, axis=1)


# ---------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------

# Fixed for every map, so that scenarios stay comparable at a glance. Font sizes are
# deliberately not set here: they come from the plt.rcParams of the notebook.
_FIGSIZE = (10, 10)
_DPI = 300
_TITLE_FONTSIZE = 12
_BOUNDARIES = [-11, 30, 34, 71]  # lon and lat limits of the map, in degrees

# Shorter headers for the inset table, which has little room.
_SHORT_NAMES = {
    ENERGY_CAPACITY: "En. cap. ~(GWh)",
    POWER_CAPACITY: "Pow. cap. ~(GW)",
    ELECTRICITY_MIX: "Elec. mix ~(TWh)",
}


def plot_map(
    n,
    df,
    value_col,
    colors,
    hatches=None,
    groups=None,
    country=None,
    title=None,
    target_ratio=4.5,
    labelspacing_circles=2,
    labelspacing_carriers=1,
    output_dir=None,
):
    """
    Map of Europe with one pie per bus, sized by `value_col` and split by carrier.

    n            : the network, used for the bus coordinates, the fallback carrier
                   colours and the title (through n.name).
    df           : table indexed by (bus, carrier), from get_storage_capacity or
                   get_electricity_mix.
    value_col    : column of `df` to plot. Its "<quantity> (<unit>)" name also gives
                   the unit of the legend, the default title and the name of the file
                   ("Energy capacity (GWh)" -> energy_capacity_map_<scenario>.png).
    colors       : {carrier: colour}, which also fixes the order of the legend.
                   Carriers missing from it fall back to the colours of the network.
    hatches      : optional {carrier: hatch pattern}, to tell apart technologies that
                   share a colour.
    groups, country : passed to summary_by_carrier for the table drawn on the map;
                   `country` adds a column with the totals of that country.
    title        : title without the scenario name, which is always appended.
    target_ratio : size of the largest bubble; lower it if bubbles overlap too much.
    output_dir   : if given, the figure is also saved there as a png.
    """
    hatches = hatches or {}
    quantity, unit = value_col.rstrip(")").split(" (")

    # Only strictly positive values are drawn as wedges, in the order of the index.
    # Dropping the others here keeps the two passes below aligned with what is drawn.
    bus_sizes = df[value_col]
    bus_sizes = bus_sizes[bus_sizes > 0]
    bus_colors = {
        c: colors.get(c, n.carriers.color.get(c, "#888888"))
        for c in df.index.get_level_values("carrier").unique()
    }

    # The size of the bubbles and the circles of the legend follow the data of the
    # scenario being plotted, so that maps stay readable whatever their scale.
    bus_size_factor, legend_circles = _scale_bus_sizes(bus_sizes, target_ratio)

    # The size and the extent of the figure are fixed before anything is drawn: cartopy
    # keeps adjusting the position of a map to its aspect ratio every time the figure is
    # rendered, which would otherwise move the legends and the table around when saving.
    fig, ax = plt.subplots(
        figsize=_FIGSIZE, dpi=_DPI, subplot_kw={"projection": ccrs.EqualEarth()}
    )
    ax.set_extent(_BOUNDARIES, crs=ccrs.PlateCarree())

    artists = n.plot(
        geomap=True,
        bus_sizes=bus_sizes / bus_size_factor,
        bus_colors=bus_colors,
        branch_components=[],  # the grid is not shown on these maps
        ax=ax,
        boundaries=_BOUNDARIES,
    )

    nodes = _node_collection(artists, ax)
    _add_bus_rings(ax, nodes, bus_sizes.index.get_level_values("bus"))
    if hatches:
        _apply_hatches(ax, nodes, bus_sizes.index.get_level_values("carrier"), hatches)

    ax.set_title(f"{title or quantity} - {n.name}", fontsize=_TITLE_FONTSIZE)

    add_legend_circles(
        ax,
        sizes=[size / bus_size_factor for size in legend_circles],
        labels=[f"{_format_number(size)} {unit}" for size in legend_circles],
        srid=n.srid,
        patch_kw={"facecolor": "lightgrey"},
        legend_kw={
            "loc": "upper right",
            "bbox_to_anchor": (1, 1),
            "title": quantity,
            "labelspacing": labelspacing_circles,
            "handletextpad": 0.75,
            "borderpad": 1.5,
            "frameon": True,
            "facecolor": "white",
            "framealpha": 1,
            "edgecolor": "grey",
        },
    )

    plotted = list(dict.fromkeys(bus_sizes.index.get_level_values("carrier")))
    ordered = [c for c in colors if c in plotted] + [
        c for c in plotted if c not in colors
    ]
    ax.legend(
        handles=[
            Patch(
                facecolor=bus_colors[c],
                edgecolor="black",
                hatch=hatches.get(c, ""),
                label=c,
            )
            for c in ordered
        ],
        loc="lower right",
        bbox_to_anchor=(1, 0),
        title="Carrier",
        labelspacing=labelspacing_carriers,
        handletextpad=0.75,
        ncol=1,
        frameon=True,
        facecolor="white",
        framealpha=1,
        edgecolor="grey",
    )

    # Drawn last, once every other element of the map is in place - see _settle_layout.
    _settle_layout(fig)
    _add_summary_table(fig, ax, _summary_for_table(n, df, groups, country))

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        name = quantity.lower().replace(" ", "_")
        plt.savefig(f"{output_dir}/{name}_map_{n.name}.png", bbox_inches="tight")
    plt.show()


def _summary_for_table(n, df, groups, country):
    """Totals per carrier for Europe and, if asked for, for one country, side by side."""
    totals = {"All": summary_by_carrier(n, df, groups)}
    if country:
        totals[country] = summary_by_carrier(n, df, groups, country=country)

    table = pd.concat(totals, axis=1).swaplevel(0, 1, axis=1)

    return (
        table.reindex(columns=pd.MultiIndex.from_product([df.columns, list(totals)]))
        .fillna(0)
        .round(0)
    )


def _scale_bus_sizes(values, target_ratio, fractions=(1, 0.1, 0.01)):
    """
    Bubble scaling factor and legend circles, derived from the data being plotted.

    Totals per bus can differ several fold between scenarios (2030 against 2050, say),
    so a fixed scaling makes bubbles either invisible or overlapping. The largest bus
    is instead always drawn at the same size, set by `target_ratio`, and the legend
    shows round fractions of that same largest value.
    """
    largest = values[values > 0].max() if (values > 0).any() else 0
    if largest <= 0:
        return 1.0, [1]

    circles = sorted({_round_down(largest * f) for f in fractions}, reverse=True)

    return largest / target_ratio, [c for c in circles if c > 0]


def _round_down(x):
    """
    Largest round number not above x - one or five times a power of ten - so that
    legends read 1,000 / 500 / 100 rather than 1,037 / 518 / 104.
    """
    if x <= 0:
        return 0

    base = 10 ** math.floor(math.log10(x))

    return 5 * base if 5 * base <= x else base


def _format_number(x):
    """Format a legend value with thousands separators, without decimals above one."""
    return f"{x:,.0f}" if x >= 1 else f"{x:g}"


def _node_collection(artists, ax):
    """The collection of pie wedges returned by n.plot, whatever the PyPSA version."""
    if isinstance(artists, dict):
        nodes = artists.get("nodes", {})
        if isinstance(nodes, dict) and nodes:
            collection = next(iter(nodes.values()))
            if isinstance(collection, PatchCollection):
                return collection

    return next((c for c in ax.collections if isinstance(c, PatchCollection)), None)


def _add_bus_rings(ax, nodes, buses, edgecolor="white", linewidth=1.3, zorder=6):
    """
    Draw a plain circle around the whole pie of each bus.

    Where buses are close to each other (Benelux, Germany) large bubbles overlap and
    blend into a single blob. One ring per bus, drawn on top of everything, marks
    where each pie ends. It has to be a separate circle rather than an outline of the
    wedges, since hatched wedges need a black edge of their own for contrast.
    """
    if nodes is None or not nodes.get_paths():
        return

    extents = {}
    for bus, path in zip(buses, nodes.get_paths()):
        box = path.get_extents()
        previous = extents.get(bus, box)
        extents[bus] = previous.union([previous, box])

    for box in extents.values():
        radius = max(box.width, box.height) / 2
        if radius > 0:
            ax.add_patch(
                Circle(
                    (box.x0 + box.width / 2, box.y0 + box.height / 2),
                    radius,
                    transform=nodes.get_transform(),
                    facecolor="none",
                    edgecolor=edgecolor,
                    linewidth=linewidth,
                    zorder=zorder,
                )
            )


def _apply_hatches(ax, nodes, carriers, hatches):
    """
    Redraw the pie wedges one by one, so that each carrier can carry its hatch.

    A hatch is only visible against the edge colour of the patch, so hatched wedges
    are given a black edge. The original collection cannot be styled wedge by wedge
    and is replaced.
    """
    if nodes is None or not nodes.get_paths():
        return

    facecolors = nodes.get_facecolors()
    edgecolors = nodes.get_edgecolors()
    linewidths = nodes.get_linewidths()

    def of_wedge(values, i, default):
        """Colours and widths are given once per wedge, or once for all of them."""
        return values[i % len(values)] if len(values) else default

    for i, (carrier, path) in enumerate(zip(carriers, nodes.get_paths())):
        hatch = hatches.get(carrier, "")
        linewidth = of_wedge(linewidths, i, 0.0)
        patch = PathPatch(
            path,
            facecolor=of_wedge(facecolors, i, "none"),
            edgecolor="black" if hatch else of_wedge(edgecolors, i, "none"),
            linewidth=max(linewidth, 0.4) if hatch else linewidth,
            hatch=hatch,
            transform=nodes.get_transform(),
            zorder=nodes.get_zorder(),
        )
        patch.set_alpha(nodes.get_alpha())
        ax.add_patch(patch)

    nodes.remove()


def _settle_layout(fig, passes=8):
    """
    Render the figure a few times, cheaply, to freeze the position of the map.

    cartopy corrects the aspect ratio of a map every time the figure is rendered, and
    each correction only gets partway to its final position. Without this, the first
    real save would still be moving the map - and with it the title the summary table
    is aligned to. Must be called once everything else has been drawn.
    """
    buffer = io.BytesIO()
    for _ in range(passes):
        buffer.seek(0)
        buffer.truncate()
        fig.savefig(buffer, format="png", dpi=50, bbox_inches="tight")


def _add_summary_table(fig, ax, table, fontsize=10, padding=0.15):
    """
    Draw `table` (with two levels of columns) as an inset in the corner of the map.

    Every column is made as wide as the text it holds, measured on the figure itself,
    and the two levels of headers are merged into one label per group. The inset is
    placed just below the title of the map.
    """
    if table is None or table.empty:
        return

    group_colors = ["#e8e8e8", "#d0d8e8"]
    groups = list(table.columns.get_level_values(0))
    subcolumns = list(table.columns.get_level_values(1))
    rows = table.index.tolist()

    unique_groups = list(dict.fromkeys(groups))
    color_of = {
        g: group_colors[i % len(group_colors)] for i, g in enumerate(unique_groups)
    }

    # Two header rows (the groups, then the columns) and one row per carrier. The
    # first column holds the carrier names: matplotlib's own row labels are drawn
    # outside the table and would be cut off at the edge of the figure.
    cells = [
        [""] * (len(subcolumns) + 1),
        [""] + subcolumns,
        *(
            [label] + [f"{v:,.0f}" for v in values]
            for label, values in zip(rows, table.values)
        ),
    ]

    renderer = fig.canvas.get_renderer()

    def text_width(text):
        artist = fig.text(0, 0, text, fontsize=fontsize, fontweight="bold")
        width = artist.get_window_extent(renderer).width / fig.dpi
        artist.remove()
        return width

    index_width = max(text_width(s) for s in [""] + rows) + padding
    widths = [
        max(
            text_width(s)
            for s in [subcolumns[i]] + [f"{v:,.0f}" for v in table.values[:, i]]
        )
        + padding
        for i in range(len(subcolumns))
    ]

    # A merged header can be wider than the columns below it: share the missing space.
    for group in unique_groups:
        columns = [i for i, g in enumerate(groups) if g == group]
        missing = (
            text_width(_SHORT_NAMES.get(group, group))
            + padding
            - sum(widths[i] for i in columns)
        )
        if missing > 0:
            for i in columns:
                widths[i] += missing / len(columns)

    row_height = 1.4 * fontsize / 72  # inches, slightly taller than the text itself
    width, height = index_width + sum(widths), row_height * len(cells)

    # Right below the title of the map, mirroring the legend in the opposite corner.
    top = (
        fig.transFigure.inverted().transform(
            (0, ax.title.get_window_extent(renderer).y0)
        )[1]
        - 0.01
    )
    inset = fig.add_axes(
        [
            0.01,
            top - height / fig.get_size_inches()[1],
            width / fig.get_size_inches()[0],
            height / fig.get_size_inches()[1],
        ]
    )
    inset.axis("off")

    fractions = [index_width / width] + [w / width for w in widths]
    grid = inset.table(
        cellText=cells, cellLoc="center", colWidths=fractions, bbox=[0, 0, 1, 1]
    )
    grid.auto_set_font_size(False)
    grid.set_fontsize(fontsize)
    grid.set_zorder(2)

    for (row, column), cell in grid.get_celld().items():
        cell.set_edgecolor("grey")
        if column == 0:
            cell.set_text_props(fontweight="bold")
            cell.set_facecolor(group_colors[0])
        elif row == 0:
            # Left transparent on purpose: a cell whose borders are partly hidden, as
            # needed below to merge cells, is filled along its border instead of over
            # its whole area. The background of this row is drawn as a rectangle.
            cell.set_facecolor("none")
        elif row == 1:
            cell.set_facecolor(color_of[groups[column - 1]])
            cell.set_text_props(fontweight="bold")

    # Merge the cells of each group: hide the borders between them, then draw the
    # background and the label of the group across the whole span.
    edges = [0]
    for fraction in fractions:
        edges.append(edges[-1] + fraction)

    for group in unique_groups:
        columns = [i for i, g in enumerate(groups) if g == group]
        for i in columns:
            grid[0, i + 1].visible_edges = (
                "BT"
                + ("L" if i == columns[0] else "")
                + ("R" if i == columns[-1] else "")
            )

        start, end = edges[columns[0] + 1], edges[columns[-1] + 2]
        inset.add_patch(
            Rectangle(
                (start, 1 - 1 / len(cells)),
                end - start,
                1 / len(cells),
                transform=inset.transAxes,
                facecolor=color_of[group],
                edgecolor="none",
                zorder=1,
            )
        )
        inset.text(
            (start + end) / 2,
            1 - 1 / (2 * len(cells)),
            _SHORT_NAMES.get(group, group),
            ha="center",
            va="center",
            fontsize=fontsize,
            fontweight="bold",
            transform=inset.transAxes,
            zorder=3,
        )


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------


def plot_balance(
    balance,
    periods,
    country,
    name,
    colors,
    hatches=None,
    storage_carriers=(),
    threshold=0.01,
    output_dir=None,
):
    """
    Electricity balance over time, one panel per period, sharing one legend.

    balance          : carriers by time steps, in GW, from get_electricity_balance.
    periods          : {name shown above the panel: ("MM-DD", "MM-DD")}, e.g.
                       {"January": ("01-01", "01-10")}. The year is ignored, so the
                       same periods can be used for any weather year.
    country, name    : only used for the title and the name of the saved file; `name`
                       is the name of the network, "<scenario> | <run>".
    colors           : {carrier: colour}, which also fixes the stacking order.
    hatches          : optional {carrier: hatch pattern}.
    storage_carriers : carriers to list separately in the legend, under storage.
    threshold        : carriers that stay below this value (GW) are left out, to keep
                       the legend readable.
    """
    fig, axes = plt.subplots(
        1, len(periods), figsize=(10 * len(periods), 10), sharey=True
    )
    axes = list(axes) if len(periods) > 1 else [axes]

    for i, (ax, (period, (start, end))) in enumerate(zip(axes, periods.items())):
        days = balance.columns.strftime("%m-%d")
        _plot_balance_panel(
            ax,
            balance.loc[:, (days >= start) & (days <= end)],
            colors,
            hatches or {},
            storage_carriers,
            period,
            threshold,
        )
        if i > 0:
            ax.set_ylabel("")

    # One legend for the whole figure, taken from the first panel.
    legend = axes[0].get_legend()
    handles = legend.legend_handles
    labels = [text.get_text() for text in legend.get_texts()]
    for ax in axes:
        ax.get_legend().remove()

    fig.suptitle(
        f"{country} electricity balance — {name}", fontsize=14, fontweight="bold"
    )
    fig.legend(
        handles,
        labels,
        bbox_to_anchor=(1.01, 0.95),
        loc="upper left",
        fontsize=10,
        frameon=True,
        edgecolor="black",
    )

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        plt.savefig(
            f"{output_dir}/time_series_{country}_{name}.png",
            dpi=300,
            bbox_inches="tight",
        )
    plt.show()


def _plot_balance_panel(
    ax, balance, colors, hatches, storage_carriers, title, threshold
):
    """One panel of plot_balance: stacked areas, with electricity demand as a line."""
    balance = balance.loc[balance.abs().max(axis=1) > threshold].astype(float)

    # Demand is what the areas have to meet, so it is drawn as a line on top of them
    # rather than stacked with them. It is a withdrawal, hence the change of sign.
    demand = "gross consumption"
    areas = balance.drop(index=demand, errors="ignore")

    # Stacking order: as in `colors`, with anything unexpected left at the end.
    ordered = [c for c in colors if c in areas.index] + [
        c for c in areas.index if c not in colors
    ]
    areas = areas.loc[ordered]

    # Feeding in and withdrawing are stacked separately, above and below zero.
    positive = ax.stackplot(
        areas.columns, areas.clip(lower=0).values, baseline="zero", zorder=1
    )
    negative = ax.stackplot(
        areas.columns, areas.clip(upper=0).values, baseline="zero", zorder=1
    )

    handles, storage_handles = [], []
    for carrier, above, below in zip(areas.index, positive, negative):
        color = colors.get(carrier, "#999999")
        hatch = hatches.get(carrier, "")
        for collection in (above, below):
            collection.set(
                facecolor=color, edgecolor="black", linewidth=0.4, hatch=hatch
            )

        patch = Patch(facecolor=color, edgecolor="black", hatch=hatch, label=carrier)
        (storage_handles if carrier in storage_carriers else handles).append(patch)

    # Headers and spacer are empty patches: matplotlib legends have no section titles.
    header = Patch(
        facecolor="none", edgecolor="none", label="--- Generation and consumption ---"
    )
    handles.insert(0, header)

    if demand in balance.index:
        color = colors.get(demand, "black")
        ax.plot(
            balance.columns,
            -balance.loc[demand],
            color=color,
            linestyle="--",
            linewidth=2,
            label=demand,
            zorder=3,
        )
        handles.append(
            Line2D([0], [0], color=color, linestyle="--", linewidth=2, label=demand)
        )

    if storage_handles:
        handles.append(Patch(facecolor="none", edgecolor="none", label=""))
        handles.append(
            Patch(facecolor="none", edgecolor="none", label="--- Storage & EVs ---")
        )
        handles.extend(storage_handles)

    ax.legend(handles=handles, bbox_to_anchor=(1.01, 1.01), loc="upper left")
    ax.axhline(0, color="black", linewidth=0.8, zorder=2)
    ax.set(ylabel="Energy balance (GW)", title=title)
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d"))
    ax.margins(x=0)

    return ax
