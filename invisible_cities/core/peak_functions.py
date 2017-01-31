"""Functions to find peaks, S12 selection etc.
JJGC and GML December 2016
"""
from __future__ import print_function, division, absolute_import

import math
import numpy as np
import pandas as pd
from time import time
import tables as tb
import matplotlib.pyplot as plt

from scipy import signal

import invisible_cities.core.system_of_units as units
import invisible_cities.sierpe.blr as blr
import invisible_cities.core.peak_functions_c as pf
from invisible_cities.database import load_db

def calibrated_pmt_sum(CWF, adc_to_pes, n_MAU=200, thr_MAU=5):
    """Compute the ZS calibrated sum of the PMTs
    after correcting the baseline with a MAU to suppress low frequency noise.
    input:
    CWF         : Corrected waveform (passed by BLR)
    adc_to_pes  : a vector with calibration constants
    n_MAU       : length of the MAU window
    thr_MAU     : treshold above MAU to select sample

    NB: This function is used mainly for testing purposes. It is
    programmed "c-style", which is not necesarily optimal in python,
    but follows the same logic that the corresponding cython function
    (in peak_functions_c), which runs faster and should be used
    instead of this one for nornal calculations.
    """

    NPMT = CWF.shape[0]
    NWF  = CWF.shape[1]
    MAU  = np.array(np.ones(n_MAU), dtype=np.double) * (1 / n_MAU)

    pmt_thr = np.zeros((NPMT, NWF), dtype=np.double)
    csum    = np.zeros(       NWF,  dtype=np.double)
    MAU_pmt = np.zeros(       NWF,  dtype=np.double)

    for j in range(NPMT):
        # MAU for each of the PMTs, following the waveform
        MAU_pmt = signal.lfilter(MAU, 1, CWF[j,:])

        for k in range(NWF):
            if CWF[j,k] >= MAU_pmt[k] + thr_MAU: # >= not >. Found testing
                pmt_thr[j,k] = CWF[j,k]

    for j in range(NPMT):
        for k in range(NWF):
            csum[k] += pmt_thr[j, k] * 1 / adc_to_pes[j]
    return csum


def wfzs(wf, threshold=0):
    """Takes a waveform wf and return the values of the wf above
    threshold: if the input waveform is of the form [e1,e2,...en],
    where ei is the energy of sample i, then then the algorithm
    returns a vector [e1,e2...ek], where k <=n and ei > threshold and
    a vector of indexes [i1,i2...ik] which label the position of the
    zswf of [e1,e2...ek]

    For example if the input waveform is:
    [1,2,3,5,7,8,9,9,10,9,8,5,7,5,6,4,1] and the trhesold is 5
    then the algoritm returns
    a vector of amplitudes [7,8,9,9,10,9,8,7,6] and a vector of indexes
    [4,5,6,7,8,9,10,12,14]

    NB: This function is used mainly for testing purposed. It is
    programmed "c-style", which is not necesarily optimal in python,
    but follows the same logic that the corresponding cython function
    (in peak_functions_c), which runs faster and should be used
    instead of this one for nornal calculations.
    """
    len_wf = wf.shape[0]
    wfzs_e = np.zeros(len_wf, dtype=np.double)
    wfzs_i = np.zeros(len_wf, dtype=np.int32)
    j=0
    for i in range(len_wf):
        if wf[i] > threshold:
            wfzs_e[j] = wf[i]
            wfzs_i[j] =    i
            j += 1

    wfzs_ene  = np.zeros(j, dtype=np.double)
    wfzs_indx = np.zeros(j, dtype=np.int32)

    for i in range(j):
        wfzs_ene [i] = wfzs_e[i]
        wfzs_indx[i] = wfzs_i[i]

    return wfzs_ene, wfzs_indx


def time_from_index(indx):
    """Return the times (in ns) corresponding to the indexes in indx

    NB: This function is used mainly for testing purposed. It is
    programmed "c-style", which is not necesarily optimal in python,
    but follows the same logic that the corresponding cython function
    (in peak_functions_c), which runs faster and should be used
    instead of this one for nornal calculations.
    """
    len_indx = indx.shape[0]
    tzs = np.zeros(len_indx, dtype=np.double)

    step = 25 #ns
    for i in range(len_indx):
        tzs[i] = step * float(indx[i])

    return tzs


def rebin_waveform(t, e, stride=40):
    """
    Rebin a waveform according to stride
    The input waveform is a vector such that the index expresses time bin and the
    contents expresses energy (e.g, in pes)
    The function returns the rebinned T and E vectors

    NB: This function is used mainly for testing purposed. It is programmed "c-style", which is not necesarily optimal
    in python, but follows the same logic that the corresponding cython
    function (in peak_functions_c), which runs faster and should be used
    instead of this one for nornal calculations.

    """

    assert len(t) == len(e)

    n = len(t) // stride
    r = len(t) %  stride

    lenb = n
    if r > 0:
        lenb = n+1

    T = np.zeros(lenb, dtype=np.double)
    E = np.zeros(lenb, dtype=np.double)

    j = 0
    for i in range(n):
        esum = 0
        tmean = 0
        for k in range(j, j + stride):
            esum  += e[k]
            tmean += t[k]

        tmean /= stride
        E[i] = esum
        T[i] = tmean
        j += stride

    if r > 0:
        esum  = 0
        tmean = 0
        for k in range(j, len(t)):
            esum  += e[k]
            tmean += t[k]
        tmean /= (len(t) - j)
        E[n] = esum
        T[n] = tmean

    return T, E


def find_S12(wfzs, index,
             tmin = 0, tmax = 1e+6,
             lmin = 8, lmax = 1000000,
             stride=4, rebin=False, rebin_stride=40):
    """
    Find S1/S2 peaks.
    input:
    wfzs:   a vector containining the zero supressed wf
    indx:   a vector of indexes
    returns a dictionary

    do not interrupt the peak if next sample comes within stride
    accept the peak only if within [lmin, lmax)
    accept the peak only if within [tmin, tmax)
    returns a dictionary of S12

    NB: This function is used mainly for testing purposed. It is programmed "c-style", which is not necesarily optimal
    in python, but follows the same logic that the corresponding cython
    function (in peak_functions_c), which runs faster and should be used
    instead of this one for nornal calculations.
    """

    P = wfzs
    T = time_from_index(index)

    assert len(wfzs) == len(index)

    S12  = {}
    S12L = {}
    s12  = []

    S12[0] = s12
    S12[0].append([T[0], P[0]])

    j = 0
    for i in range(1, len(wfzs)) :

        if T[i] > tmax:
            break

        if T[i] < tmin:
            continue

        if index[i] - stride > index[i-1]:  #new s12
            j += 1
            s12 = []
            S12[j] = s12
        S12[j].append([T[i], P[i]])

    # re-arrange and rebin
    j = 0
    for i in S12:
        ls = len(S12[i])

        if not (lmin <= ls < lmax):
            continue

        t = np.zeros(ls, dtype=np.double)
        e = np.zeros(ls, dtype=np.double)

        for k in range(ls):
            t[k] = S12[i][k][0]
            e[k] = S12[i][k][1]

        if rebin == True:
            TR, ER = rebin_waveform(t, e, stride = rebin_stride)
            S12L[j] = [TR, ER]
        else:
            S12L[j] = [t, e]
        j += 1

    return S12L
