"""
-----------------------------------------------------------------------
                              Beersheba
-----------------------------------------------------------------------
Beersheba, a city suspended from the heavens, inhabited only by idealists.
This city interpolates corrected hits and applies Lucy-Richardson deconvolution
to the interpolated signal.
The input is esmeralda output containing hits, kdst global information and mc info.
The city outputs :
    - DECO deconvolved hits table
    - MC info (if run number <=0)
    - SUMMARY summary of per event information
"""

import numpy  as np
import tables as tb
import pandas as pd

from os   .path  import expandvars
from scipy.stats import multivariate_normal
from numpy       import nan_to_num

from typing      import Tuple
from typing      import List
from typing import Callable

from enum        import auto

from .  components import city
from .  components import collect
from .  components import copy_mc_info
from .  components import print_every
from .  components import cdst_and_kdst_from_files

from .  esmeralda  import summary_writer
from .  esmeralda  import track_writer
from .  esmeralda  import kdst_from_df_writer

from .. reco                   import tbl_functions           as tbl
from .. dataflow               import dataflow                as fl

from .. dataflow.dataflow      import push
from .. dataflow.dataflow      import pipe

from .. reco.deconv_functions  import find_nearest
from .. reco.deconv_functions  import cut_and_redistribute_df
from .. reco.deconv_functions  import drop_isolated_sensors
from .. reco.deconv_functions  import deconvolve
from .. reco.deconv_functions  import richardson_lucy
from .. reco.deconv_functions  import InterpolationMethod

from .. io.run_and_event_io    import run_and_event_writer
from .. io.          dst_io    import df_writer
from .. io.          dst_io    import load_dst

from .. evm.event_model        import HitEnergy

from .. evm                    import event_model as evm
from .. reco                   import paolina_functions as plf

from .. types.ic_types         import AutoNameEnumBase
from .. types.ic_types         import xy

from .. core                   import system_of_units as units


def convert_true_to_hits(x, y, z, e):
    return [evm.Hit(0, evm.Cluster(0, xy(xx, yy), xy(0,0), 0), zz, ee, xy(0, 0))
            for xx, yy, zz, ee in zip(x, y, z, e)]


def track_blob_info_creator_extractor(vox_size : [float, float, float],
                                      energy_type : evm.HitEnergy,
                                      strict_vox_size : bool,
                                      energy_threshold : float,
                                      min_voxels : int,
                                      blob_radius : float) -> Callable:
    """ Wrapper of extract_track_blob_info"""
    def create_extract_track_blob_info(hitcol):
        """This function extract relevant info about the tracks and blobs, as well as assigning new field of energy, track_id etc to the HitCollection object (NOTE: we don't want to erase any hits, just redifine some attributes. If we need to cut away some hits to apply paolina functions, it has to be on the copy of the original hits)"""

        hitc = evm.HitCollection(hitcol.event.unique()[0], 0)
        hitc.hits = convert_true_to_hits(hitcol['X'].values, hitcol['Y'].values, hitcol['Z'].values, hitcol['E'].values) 

        voxels     = plf.voxelize_hits(hitc.hits, vox_size, strict_vox_size, energy_type)
        if energy_threshold == 0.:
            mod_voxels = voxels
        else:
            (mod_voxels, dropped_voxels) = plf.drop_end_point_voxels(voxels, energy_threshold, min_voxels)
        tracks     = plf.make_track_graphs(mod_voxels)

        vox_size_x = voxels[0].size[0]
        vox_size_y = voxels[0].size[1]
        vox_size_z = voxels[0].size[2]

        #sort tracks in energy
        tracks     = sorted(tracks, key = lambda t: sum([vox.Ehits for vox in t.nodes()]), reverse = True)

        track_hits = []
        df = pd.DataFrame(columns=['event', 'trackID', 'energy', 'length', 'numb_of_voxels',
                                   'numb_of_hits', 'numb_of_tracks', 'x_min', 'y_min', 'z_min',
                                   'x_max', 'y_max', 'z_max', 'r_max', 'x_ave', 'y_ave', 'z_ave',
                                   'extreme1_x', 'extreme1_y', 'extreme1_z',
                                   'extreme2_x', 'extreme2_y', 'extreme2_z',
                                   'blob1_x', 'blob1_y', 'blob1_z',
                                   'blob2_x', 'blob2_y', 'blob2_z',
                                   'eblob1', 'eblob2', 'ovlp_blob_energy',
                                   'vox_size_x', 'vox_size_y', 'vox_size_z'])
        hitsblob1 = []
        hitsblob2 = []
        for c, t in enumerate(tracks, 0):
            tID = c
            energy = sum([vox.Ehits for vox in t.nodes()])
            length = plf.length(t)
            numb_of_hits = len([h for vox in t.nodes() for h in vox.hits])
            numb_of_voxels = len(t.nodes())
            numb_of_tracks = len(tracks   )

            min_x = min([h.X for v in t.nodes() for h in v.hits])
            max_x = max([h.X for v in t.nodes() for h in v.hits])
            min_y = min([h.Y for v in t.nodes() for h in v.hits])
            max_y = max([h.Y for v in t.nodes() for h in v.hits])
            min_z = min([h.Z for v in t.nodes() for h in v.hits])
            max_z = max([h.Z for v in t.nodes() for h in v.hits])
            max_r = max([np.sqrt(h.X*h.X + h.Y*h.Y) for v in t.nodes() for h in v.hits])

            pos = [h.pos for v in t.nodes() for h in v.hits]
            e   = [getattr(h, energy_type.value) for v in t.nodes() for h in v.hits]
            ave_pos = np.average(pos, weights=e, axis=0)

            extr1, extr2 = plf.find_extrema(t)
            extr1_pos = extr1.XYZ
            extr2_pos = extr2.XYZ

            blob_pos1, blob_pos2 = plf.blob_centres(t, blob_radius)

            e_blob1, e_blob2, hits_blob1, hits_blob2 = plf.blob_energies_and_hits(t, blob_radius)
            hitsblob1.append(hits_blob1)
            hitsblob2.append(hits_blob2)
            overlap = float(sum([h.E for h in set(hits_blob1).intersection(hits_blob2)]))
            list_of_vars = [hitc.event, tID, energy, length, numb_of_voxels, numb_of_hits, numb_of_tracks, min_x, min_y, min_z, max_x, max_y, max_z, max_r, ave_pos[0], ave_pos[1], ave_pos[2], extr1_pos[0], extr1_pos[1], extr1_pos[2], extr2_pos[0], extr2_pos[1], extr2_pos[2], blob_pos1[0], blob_pos1[1], blob_pos1[2], blob_pos2[0], blob_pos2[1], blob_pos2[2], e_blob1, e_blob2, overlap, vox_size_x, vox_size_y, vox_size_z]

            df.loc[c] = list_of_vars
            try:
                types_dict
            except NameError:
                types_dict = dict(zip(df.columns, [type(x) for x in list_of_vars]))

            for vox in t.nodes():
                for hit in vox.hits:
                    hit.track_id = tID
                    track_hits.append(hit)


        track_hitc = evm.HitCollection(hitc.event, hitc.time)
        track_hitc.hits = track_hits
        #change dtype of columns to match type of variables
        df = df.apply(lambda x : x.astype(types_dict[x.name]))

        return df, track_hitc, mod_voxels

    return create_extract_track_blob_info


class CutType          (AutoNameEnumBase):
    abs = auto()
    rel = auto()

class DeconvolutionMode(AutoNameEnumBase):
    joint    = auto()
    separate = auto()


def deconvolve_signal(psf_fname       : str,
                      e_cut           : float,
                      n_iterations    : int,
                      iteration_tol   : float,
                      sample_width    : List[float],
                      bin_size        : List[float],
                      diffusion       : Tuple[float]=(1., 1., 0.3),
                      energy_type     : HitEnergy=HitEnergy.Ec,
                      deconv_mode     : DeconvolutionMode=DeconvolutionMode.joint,
                      n_dim           : int=2,
                      cut_type        : CutType=CutType.abs,
                      inter_method    : InterpolationMethod=InterpolationMethod.cubic,
                      n_iterations_g  : int=0):

    """
    Applies Lucy Richardson deconvolution to SiPM response with a
    given set of PSFs and parameters.

    Parameters
    ----------
    psf_fname       : Point-spread function.
    e_cut           : Cut in relative value to the max voxel over the deconvolution output.
    n_iterations    : Number of Lucy-Richardson iterations
    iteration_tol   : Stopping threshold (difference between iterations).
    sample_width    : Sampling size of the sensors.
    bin_size        : Size of the interpolated bins.
    energy_type     : Energy type ('E' or 'Ec', see Esmeralda) used for assignment.
    deconv_mode     : 'joint' or 'separate', 1 or 2 step deconvolution, see description later.
    diffusion       : Diffusion coefficients in each dimension for 'separate' mode.
    n_dim           : Number of dimensions to apply the method (usually 2).
    cut_type        : Cut mode to the deconvolution output ('abs' or 'rel') using e_cut
                      'abs': cut on the absolute value of the hits.
                      'rel': cut on the relative value (to the max) of the hits.
    inter_method    : Interpolation method.
    n_iterations_g  : Number of Lucy-Richardson iterations for gaussian in 'separate mode'

    Returns
    ----------
    apply_deconvolution : Function that takes hits and returns the
    deconvolved data.
    """
    dimensions    = np.array  (['X', 'Y', 'Z'][:n_dim])
    sample_width  = np.asarray(sample_width           )
    bin_size      = np.asarray(bin_size               )
    diffusion     = np.asarray(diffusion              )

    psfs          = load_dst(psf_fname, 'PSF', 'PSFs')
    deconvolution = deconvolve(n_iterations, iteration_tol, sample_width, bin_size, inter_method)

    if not isinstance(energy_type , HitEnergy          ):
        raise ValueError(f'energy_type {energy_type} is not a valid energy type.')
    if not isinstance(inter_method, InterpolationMethod):
        raise ValueError(f'inter_method {inter_method} is not a valid interpolation method.')
    if not isinstance(cut_type    , CutType            ):
        raise ValueError(f'cut_type {cut_type} is not a valid cut type.')
    if not isinstance(deconv_mode , DeconvolutionMode  ):
        raise ValueError(f'deconv_mode {deconv_mode} is not a valid deconvolution mode.')

    def deconvolve_hits(df, z):
        '''
        Given an slice, applies deconvolution using the PSF
        associated to the passed z.

        Parameters
        ----------
        df : Original input dataframe for the deconvolution (single slice cdst)
        z  : Longitudinal position of the slice.
        Returns
        ----------
        Dataframe with the deconvolved slice.
        '''
        xx, yy = df.Xpeak.unique(), df.Ypeak.unique()
        zz     = z if deconv_mode is DeconvolutionMode.joint else 0
        psf = psfs.loc[(psfs.z == find_nearest(psfs.z, zz)) &
                       (psfs.x == find_nearest(psfs.x, xx)) &
                       (psfs.y == find_nearest(psfs.y, yy)) , :]
        deconv_image, pos = deconvolution(tuple(df.loc[:, dimensions].values.T), df.NormQ.values, psf)

        if   deconv_mode is DeconvolutionMode.joint:
            pass
        elif deconv_mode is DeconvolutionMode.separate:
            dist         = multivariate_normal(np.zeros(n_dim), diffusion**2 * z * units.mm / units.cm) #Z is in mm in cdst
            cols         = tuple(f"{v.lower()}r" for v in dimensions)
            psf_cols     = psf.loc[:, cols]
            gaus         = dist.pdf(psf_cols.values)
            psf          = gaus.reshape(psf_cols.nunique())
            deconv_image = nan_to_num(richardson_lucy(deconv_image, psf, n_iterations_g, iteration_tol))

        return create_deconvolution_df(df, deconv_image.flatten(), pos, cut_type, e_cut, n_dim)

    def apply_deconvolution(df):
        '''
        Given an event cdst, it iterates through its S2s and applies deconvolution
        to each S2.

        Parameters
        ----------
        df : Original input dataframe for the deconvolution (event cdst)

        Returns
        ----------
        Dataframe with the deconvolved event.
        '''
        deco_dst = []
        df.loc[:, "NormQ"] = np.nan
        for peak, hits in df.groupby("npeak"):
            hits.loc[:, "NormQ"] = hits.loc[:, 'Q'] / hits.loc[:, 'Q'].sum()
            deconvolved_hits = pd.concat([deconvolve_hits(df_z, z) for z, df_z in hits.groupby("Z")], ignore_index=True)
            distribute_energy(deconvolved_hits, hits, energy_type)
            deco_dst.append(deconvolved_hits)

        return pd.concat(deco_dst, ignore_index=True)

    return apply_deconvolution


def create_deconvolution_df(hits, deconv_e, pos, cut_type, e_cut, n_dim):
    '''
    Given the output of the deconvolution, it cuts the low energy voxels and
    creates a dataframe object with the resulting output.

    Parameters
    ----------
    hits     : Original input dataframe for the deconvolution (S2 cdst)
    deconv_e : Deconvolution energy distribution (n-dim array)
    pos      : Position of the deconvolved hits.
    cut_type : CutType object with the cut mode.
    e_cut    : Value for the energy cut.
    n_dim    : Number of dimensions of the deconvolution (tipically 2 as of now)

    Returns
    ----------
    df       : Dataframe with the deconvolution input after energy cutting.
    '''

    df  = pd.DataFrame(columns=['event', 'npeak', 'X', 'Y', 'Z', 'E'])

    if   cut_type is CutType.abs:
        sel_deconv = deconv_e > e_cut
    elif cut_type is CutType.rel:
        sel_deconv = deconv_e / deconv_e.max() > e_cut
    else:
        raise ValueError(f'cut_type {cut_type} is not a valid cut type.')

    df['E']     = deconv_e[sel_deconv]
    df['event'] = hits.event.unique()[0]
    df['npeak'] = hits.npeak.unique()[0]
    df['Z']     = hits.Z    .unique()[0] if n_dim == 2 else pos[2][sel_deconv]
    df['X']     = pos[0][sel_deconv]
    df['Y']     = pos[1][sel_deconv]

    return df


def distribute_energy(df, cdst, energy_type):
    '''
    Assign the energy of a dataframe (cdst) to another dataframe (deconvolved),
    distributing it according to the charge fraction of each deconvolution hit.

    Parameters
    ----------
    df          : Deconvolved dataframe with a single S2 (npeak)
    cdst        : Dataframe with the sensor response (usually a cdst)
    energy_type : HitEnergy with which 'type' of energy should be assigned.
    '''
    df.loc[:, 'E'] = df.E / df.E.sum() * cdst.loc[:, energy_type.value].sum()


def cut_over_Q(q_cut, redist_var):
    '''
    Apply a cut over the SiPM charge condition to hits and redistribute the
    energy variables.

    Parameters
    ----------
    q_cut      : Charge value over which to cut.
    redist_var : List with variables to be redistributed.

    Returns
    ----------
    cut_over_Q : Function that will cut the dataframe and redistribute
    values.
    '''
    cut = cut_and_redistribute_df(f"Q > {q_cut}", redist_var)

    def cut_over_Q(df):  # df shall be an event cdst
        cdst = df.groupby(['event', 'npeak']).apply(cut).reset_index(drop=True)

        return cdst

    return cut_over_Q


def drop_isolated(distance, redist_var):
    """
    Drops rogue/isolated hits (SiPMs) from hits.

    Parameters
    ----------
    distance   : Sensor pitch.
    redist_var : List with variables to be redistributed.

    Returns
    ----------
    drop_isolated_sensors : Function that will drop the isolated sensors.
    """
    drop = drop_isolated_sensors(distance, redist_var)

    def drop_isolated(df): # df shall be an event cdst
        df = df.groupby(['event', 'npeak']).apply(drop).reset_index(drop=True)

        return df

    return drop_isolated


def check_nonempty_dataframe(df) -> bool:
    """
    Filter for Beersheba flow. The flow stops if:
        1. there are no hits (after droping isolated sensors)
    """
    return len(df) > 0


def deconv_writer(h5out, compression='ZLIB4'):
    """
    For a given open table returns a writer for deconvolution hits dataframe
    """
    def write_deconv(df):
        return df_writer(h5out              = h5out             ,
                         df                 = df                ,
                         compression        = compression       ,
                         group_name         = 'DECO'            ,
                         table_name         = 'Events'          ,
                         descriptive_string = 'Deconvolved hits',
                         columns_to_index   = ['event']         )
    return write_deconv


@city
def beersheba(files_in, file_out, compression, event_range, print_mod, detector_db, run_number,
              deconv_params = dict(), paolina_params = dict()):
    """
    The city corrects Penthesilea hits energy and extracts topology information.
    ----------
    Parameters
    ----------
    files_in    : str, filepath
         Input file
    file_out    : str, filepath
         Output file
    compression : str
         Default  'ZLIB4'
    event_range : int /'all_events'
         Number of events from files_in to process
    print_mode  : int
         How frequently to print events
    run_number  : int
         Has to be negative for MC runs

    deconv_params : dict
        q_cut          : float
            Minimum charge (pes) on a hit (SiPM)
        drop_dist      : float
            Distance to check if a SiPM is isolated
        psf_fname      : string (filepath)
            Filename of the psf
        e_cut          : float
            Cut over the deconvolution output, arbitrary units (order 1e-3)
        n_iterations   : int
            Number of iterations to be applied if the iteration_tol criteria
            is not fulfilled before.
        iteration_tol  : float
            Stopping threshold (difference between iterations). I
        sample_width   : list[float]
            Sampling of the sensors in each dimension (usuallly the pitch).
        bin_size       : list[float]
            Bin size (mm) of the deconvolved image.
        energy_type    : str ('E', 'Ec')
            Marks which energy from Esmeralda (E = uncorrected, Ec = corrected)
            should be assigned to the deconvolved track.
        deconv_mode    : str ('joint', 'separate')
            - 'joint' deconvolves once using a PSF based on Z that includes
               both EL and diffusion spread aproximated to a Z range.
            - 'separate' deconvolves twice, first using the EL PSF, then using
               a gaussian PSF based on the exact Z position of the slice.
        diffusion      : tuple(float)
            Diffusion coefficients in each dimmension (mm/sqrt(cm))
            used if deconv_mode is 'separate'
        n_dim          : int
            Number of dimensions used in deconvolution, currently only 2 max:
            n_dim = 2 -> slice by slice XY deconvolution.
            n_dim = 3 -> XYZ deconvolution (in the works).
        inter_method   : str (None, 'linear', 'cubic')
            Sensor interpolation method. If None, no interpolation will be applied.
            'cubic' not supported for 3D deconvolution.
        n_iterations_g : int
            Number of Lucy-Richardson iterations for gaussian in 'separate mode'

    ----------
    Input
    ----------
    Esmeralda output
    ----------
    Output
    ----------
    DECO    : Deconvolved hits table
    MC info : (if run number <=0)
    SUMMARY : Table with the summary from Esmeralda.
"""

    deconv_params['cut_type'    ] = CutType            (deconv_params['cut_type'    ])
    deconv_params['deconv_mode' ] = DeconvolutionMode  (deconv_params['deconv_mode' ])
    deconv_params['energy_type' ] = HitEnergy          (deconv_params['energy_type' ])
    deconv_params['inter_method'] = InterpolationMethod(deconv_params['inter_method'])

    deconv_params['psf_fname'   ] = expandvars(deconv_params['psf_fname'])

    paolina_params['energy_type'] = HitEnergy(paolina_params['energy_type'])

    for p in ['sample_width', 'bin_size', 'diffusion']:
        if len(deconv_params[p]) != deconv_params['n_dim']:
            raise ValueError         (f"Parameter {p} dimensions do not match n_dim parameter")
    if deconv_params['n_dim'] > 2:
        raise     NotImplementedError(f"{deconv_params['n_dim']}-dimensional PSF not yet implemented")

    cut_sensors           = fl.map(cut_over_Q   (deconv_params.pop("q_cut")    , ['E', 'Ec']),
                                   item = 'cdst')
    drop_sensors          = fl.map(drop_isolated(deconv_params.pop("drop_dist"), ['E', 'Ec']),
                                   item = 'cdst')
    filter_events_no_hits = fl.map(check_nonempty_dataframe,
                                   args = 'cdst',
                                   out  = 'cdst_passed_no_hits')
    deconvolve_events     = fl.map(deconvolve_signal(**deconv_params),
                                   args = 'cdst',
                                   out  = 'deconv_dst')
    create_extract_track_blob_info  = fl.map(track_blob_info_creator_extractor(**paolina_params),
                                             args = 'deconv_dst',
                                             out  = ('topology_info', 'paolina_hits', 'out_of_map'))

    event_count_in        = fl.spy_count()
    event_count_out       = fl.spy_count()
    events_passed_no_hits = fl.count_filter(bool, args = "cdst_passed_no_hits")

    evtnum_collect        = collect()

    with tb.open_file(file_out, "w", filters = tbl.filters(compression)) as h5out:
        # Define writers
        write_event_info = fl.sink(run_and_event_writer(h5out), args=("run_number", "event_number", "timestamp"))
        write_deconv     = fl.sink(  deconv_writer(h5out=h5out), args =  "deconv_dst"         )
        write_tracks     = fl.sink(  track_writer(h5out=h5out),  args =  "topology_info"      )
        write_kdst_table = fl.sink( kdst_from_df_writer(h5out),  args =  "kdst"               )
        write_summary    = fl.sink( summary_writer(h5out=h5out), args =  "summary"            )
        result = push(source = cdst_and_kdst_from_files(files_in),
                      pipe   = pipe(fl.slice(*event_range, close_all=True)    ,
                                    print_every(print_mod)                    ,
                                    event_count_in.spy                        ,
                                    cut_sensors                               ,
                                    drop_sensors                              ,
                                    filter_events_no_hits                     ,
                                    events_passed_no_hits    .filter          ,
                                    deconvolve_events                         ,
                                    create_extract_track_blob_info            ,
                                    event_count_out.spy                       ,
                                    fl.branch("event_number"     ,
                                              evtnum_collect.sink)            ,
                                    fl.fork(write_deconv    ,
                                            write_tracks    ,
                                            write_summary   ,
                                            write_kdst_table,
                                            write_event_info))                ,
                      result = dict(events_in   = event_count_in       .future,
                                    events_out  = event_count_out      .future,
                                    evtnum_list = evtnum_collect       .future,
                                    events_pass = events_passed_no_hits.future))

        if run_number <= 0:
            copy_mc_info(files_in, h5out, result.evtnum_list,
                         detector_db, run_number)

        return result
