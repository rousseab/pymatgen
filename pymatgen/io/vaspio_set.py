#!/usr/bin/env python

"""
This module defines the VaspInputSet abstract base class and a concrete
implementation for the parameters used by the Materials Project and the MIT
high throughput project.  The basic concept behind an input set is to specify
a scheme to generate a consistent set of VASP inputs from a structure
without further user intervention. This ensures comparability across
runs.
"""

from __future__ import division

__author__ = "Shyue Ping Ong, Wei Chen, Will Richards"
__copyright__ = "Copyright 2011, The Materials Project"
__version__ = "1.0"
__maintainer__ = "Shyue Ping Ong"
__email__ = "shyuep@gmail.com"
__date__ = "Nov 16, 2011"

import os
import abc
import json
import re
from functools import partial

from pymatgen.io.cifio import CifWriter
from pymatgen.io.vaspio.vasp_input import Incar, Poscar, Potcar, Kpoints
from pymatgen.io.vaspio.vasp_output import Vasprun, Outcar
from pymatgen.serializers.json_coders import MSONable
from pymatgen.symmetry.finder import SymmetryFinder
from pymatgen.symmetry.bandstructure import HighSymmKpath
from pymatgen import write_structure
import traceback
import numpy as np
import shutil


MODULE_DIR = os.path.dirname(os.path.abspath(__file__))


class AbstractVaspInputSet(MSONable):
    """
    Abstract base class representing a set of Vasp input parameters.
    The idea is that using a VaspInputSet, a complete set of input files
    (INPUT, KPOINTS, POSCAR and POTCAR) can be generated in an automated
    fashion for any structure.
    """
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def get_poscar(self, structure):
        """
        Returns Poscar from a structure.
        """
        return

    @abc.abstractmethod
    def get_kpoints(self, structure):
        """
        Returns Kpoints from a structure.

        Args:
            structure:
                Structure object

        Returns:
            Kpoints object
        """
        return

    @abc.abstractmethod
    def get_incar(self, structure):
        """
        Returns Incar from a structure.

        Args:
            structure:
                Structure object

        Returns:
            Incar object
        """
        return

    @abc.abstractmethod
    def get_potcar(self, structure):
        """
        Returns Potcar from a structure.

        Args:
            structure:
                Structure object

        Returns:
            Potcar object
        """
        return

    @abc.abstractmethod
    def get_potcar_symbols(self, structure):
        """
        Returns list of POTCAR symbols from a structure.

        Args:
            structure:
                Structure object

        Returns:
            List of POTCAR symbols
        """
        return

    def get_all_vasp_input(self, structure, generate_potcar=True):
        """
        Returns all input files as a dict of {filename: vaspio object}

        Args:
            structure:
                Structure object
            generate_potcar:
                Set to False to generate a POTCAR.spec file instead of a
                POTCAR, which contains the POTCAR labels but not the actual
                POTCAR. Defaults to True.

        Returns:
            dict of {filename: file_as_string}, e.g., {'INCAR':'EDIFF=1e-4...'}
        """
        d = {'INCAR': self.get_incar(structure),
             'KPOINTS': self.get_kpoints(structure),
             'POSCAR': self.get_poscar(structure)}
        if generate_potcar:
            d['POTCAR'] = self.get_potcar(structure)
        else:
            d['POTCAR.spec'] = "\n".join(self.get_potcar_symbols(structure))
        return d

    def write_input(self, structure, output_dir, make_dir_if_not_present=True):
        """
        Writes a set of VASP input to a directory.

        Args:
            structure:
                Structure object
            output_dir:
                Directory to output the VASP input files
            make_dir_if_not_present:
                Set to True if you want the directory (and the whole path) to
                be created if it is not present.
        """
        if make_dir_if_not_present and not os.path.exists(output_dir):
            os.makedirs(output_dir)
        for k, v in self.get_all_vasp_input(structure).items():
            v.write_file(os.path.join(output_dir, k))


class DictVaspInputSet(AbstractVaspInputSet):
    """
    Concrete implementation of VaspInputSet that is initialized from a dict
    settings. This allows arbitrary settings to be input. In general,
    this is rarely used directly unless there is a source of settings in JSON
    format (e.g., from a REST interface). It is typically used by other
    VaspInputSets for initialization.

    Special consideration should be paid to the way the MAGMOM initialization
    for the INCAR is done. The initialization differs depending on the type of
    structure and the configuration settings. The order in which the magmom is
    determined is as follows:

    1. If the site itself has a magmom setting, that is used.
    2. If the species on the site has a spin setting, that is used.
    3. If the species itself has a particular setting in the config file, that
       is used, e.g., Mn3+ may have a different magmom than Mn4+.
    4. Lastly, the element symbol itself is checked in the config file. If
       there are no settings, VASP's default of 0.6 is used.
    """

    def __init__(self, name, config_dict, hubbard_off=False,
                 user_incar_settings=None,
                 constrain_total_magmom=False, sort_structure=True):
        """
        Args:
            name:
                A name fo the input set.
            config_dict:
                The config dictionary to use.
            hubbard_off:
                Whether to turn off Hubbard U if it is specified in
                config_dict. Defaults to False, i.e., follow settings in
                config_dict.
            user_incar_settings:
                User INCAR settings. This allows a user to override INCAR
                settings, e.g., setting a different MAGMOM for various elements
                or species.
            constrain_total_magmom:
                Whether to constrain the total magmom (NUPDOWN in INCAR) to be
                the sum of the expected MAGMOM for all species. Defaults to
                False.
            sort_structure:
                Whether to sort the structure (using the default sort
                order of electronegativity) before generating input files.
                Defaults to True, the behavior you would want most of the
                time. This ensures that similar atomic species are grouped
                together.
        """
        self.name = name
        self.potcar_settings = config_dict["POTCAR"]
        self.kpoints_settings = config_dict['KPOINTS']
        self.incar_settings = config_dict['INCAR']
        self.set_nupdown = constrain_total_magmom
        self.sort_structure = sort_structure
        self.hubbard_off = hubbard_off
        if hubbard_off:
            for k in self.incar_settings.keys():
                if k.startswith("LDAU"):
                    del self.incar_settings[k]
        if user_incar_settings:
            self.incar_settings.update(user_incar_settings)

    def get_incar(self, structure):
        incar = Incar()
        if self.sort_structure:
            structure = structure.get_sorted_structure()
        comp = structure.composition
        elements = sorted([el for el in comp.elements if comp[el] > 0],
                          key=lambda el: el.X)
        most_electroneg = elements[-1].symbol
        poscar = Poscar(structure)
        for key, setting in self.incar_settings.items():
            if key == "MAGMOM":
                mag = []
                for site in structure:
                    if hasattr(site, 'magmom'):
                        mag.append(site.magmom)
                    elif hasattr(site.specie, 'spin'):
                        mag.append(site.specie.spin)
                    elif str(site.specie) in setting:
                        mag.append(setting.get(str(site.specie)))
                    else:
                        mag.append(setting.get(site.specie.symbol, 0.6))
                incar[key] = mag
            elif key in ('LDAUU', 'LDAUJ', 'LDAUL'):
                if most_electroneg in setting.keys():
                    incar[key] = [setting[most_electroneg].get(sym, 0)
                                  for sym in poscar.site_symbols]
                else:
                    incar[key] = [0] * len(poscar.site_symbols)
            elif key == "EDIFF":
                incar[key] = float(setting) * structure.num_sites
            else:
                incar[key] = setting

        has_u = ("LDAUU" in incar and sum(incar['LDAUU']) > 0)
        if has_u:
            # modify LMAXMIX if LSDA+U and you have d or f electrons
            # note that if the user explicitly sets LMAXMIX in settings it will
            # override this logic.
            if 'LMAXMIX' not in self.incar_settings.keys():
                # contains f-electrons
                if any([el.Z > 56 for el in structure.composition]):
                    incar['LMAXMIX'] = 6
                # contains d-electrons
                elif any([el.Z > 20 for el in structure.composition]):
                    incar['LMAXMIX'] = 4
        else:
            for key in incar.keys():
                if key.startswith('LDAU'):
                    del incar[key]

        if self.set_nupdown:
            nupdown = sum([mag if abs(mag) > 0.6 else 0 for mag in incar['MAGMOM']])
            incar['NUPDOWN'] = nupdown

        return incar

    def get_poscar(self, structure):
        if self.sort_structure:
            structure = structure.get_sorted_structure()
        return Poscar(structure)

    def get_potcar(self, structure):
        if self.sort_structure:
            structure = structure.get_sorted_structure()
        return Potcar(self.get_potcar_symbols(structure))

    def get_potcar_symbols(self, structure):
        if self.sort_structure:
            structure = structure.get_sorted_structure()
        p = self.get_poscar(structure)
        elements = p.site_symbols
        potcar_symbols = []
        for el in elements:
            potcar_symbols.append(self.potcar_settings[el]
                                  if el in self.potcar_settings else el)
        return potcar_symbols

    def get_kpoints(self, structure):
        """
        Writes out a KPOINTS file using the fully automated grid method. Uses
        Gamma centered meshes  for hexagonal cells and Monk grids otherwise.

        Algorithm:
            Uses a simple approach scaling the number of divisions along each
            reciprocal lattice vector proportional to its length.
        """
        if self.sort_structure:
            structure = structure.get_sorted_structure()
        dens = int(self.kpoints_settings['grid_density'])
        return Kpoints.automatic_density(structure, dens)

    def __str__(self):
        return self.name

    def __repr__(self):
        output = [self.name, ""]
        section_names = ['INCAR settings', 'KPOINTS settings',
                         'POTCAR settings']
        count = 0
        for d in [self.incar_settings, self.kpoints_settings,
                  self.potcar_settings]:
            output.append(section_names[count])
            for k, v in d.items():
                output.append("%s = %s" % (k, str(v)))
            output.append("")
            count += 1
        return "\n".join(output)

    @property
    def to_dict(self):
        config_dict = {
            "INCAR": self.incar_settings,
            "KPOINTS": self.kpoints_settings,
            "POTCAR": self.potcar_settings
        }
        return {
            "name": self.name,
            "config_dict": config_dict,
            "hubbard_off": self.hubbard_off,
            "constrain_total_magmom": self.set_nupdown,
            "sort_structure": self.sort_structure,
            "@class": self.__class__.__name__,
            "@module": self.__class__.__module__,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(d["name"], d["config_dict"],
                   hubbard_off=d.get("hubbard_off", False),
                   constrain_total_magmom=d["constrain_total_magmom"],
                   sort_structure=d.get("sort_structure", True))

    @staticmethod
    def from_json_file(name, json_file, hubbard_off=False,
                       user_incar_settings=None, constrain_total_magmom=False,
                       sort_structure=True):
        """
        Creates a DictVaspInputSet from a json file.

        Args:
            name:
                A name for the input set.
            json_file:
                An actual file containing the settings.
            hubbard_off:
                Whether to turn off Hubbard U if it is specified in
                config_dict. Defaults to False, i.e., follow settings in
                config_dict.
            user_incar_settings:
                User INCAR settings. This allows a user to override INCAR
                settings, e.g., setting a different MAGMOM for various elements
                or species.
            constrain_total_magmom:
                Whether to constrain the total magmom (NUPDOWN in INCAR) to be
                the sum of the expected MAGMOM for all species. Defaults to
                False.
            sort_structure:
                Whether to sort the structure (using the default sort
                order of electronegativity) before generating input files.
                Defaults to True, the behavior you would want most of the
                time. This ensures that similar atomic species are grouped
                together.
        """
        with open(json_file) as f:
            return DictVaspInputSet(
                name, json.load(f),
                hubbard_off=hubbard_off,
                constrain_total_magmom=constrain_total_magmom,
                user_incar_settings=user_incar_settings,
                sort_structure=sort_structure)


MITVaspInputSet = partial(DictVaspInputSet.from_json_file, "MIT",
                          os.path.join(MODULE_DIR, "MITVaspInputSet.json"))
"""
Standard implementation of VaspInputSet utilizing parameters in the MIT
High-throughput project.
The parameters are chosen specifically for a high-throughput project,
which means in general pseudopotentials with fewer electrons were chosen.

Please refer::

    A Jain, G. Hautier, C. Moore, S. P. Ong, C. Fischer, T. Mueller,
    K. A. Persson, G. Ceder. A high-throughput infrastructure for density
    functional theory calculations. Computational Materials Science,
    2011, 50(8), 2295-2310. doi:10.1016/j.commatsci.2011.02.023

for more information. Supports the same kwargs as :class:`JSONVaspInputSet`.
"""

MITGGAVaspInputSet = partial(DictVaspInputSet.from_json_file, "MIT GGA",
                             os.path.join(MODULE_DIR, "MITVaspInputSet.json"),
                             hubbard_off=True)
"""
GGA (no U) version of MITVaspInputSet.
Supports the same kwargs as :class:`JSONVaspInputSet`.
"""

MITHSEVaspInputSet = partial(
    DictVaspInputSet.from_json_file, "MIT HSE",
    os.path.join(MODULE_DIR, "MITHSEVaspInputSet.json"))
"""
Typical implementation of input set for a HSE run using MIT parameters.
Supports the same kwargs as :class:`JSONVaspInputSet`.
"""


class MITNEBVaspInputSet(DictVaspInputSet):
    """
    Class for writing NEB inputs.
    """

    def __init__(self, nimages=8, user_incar_settings=None, **kwargs):
        """
        Args:
            nimages:
                Number of NEB images (excluding start and ending structures).
            **kwargs:
                Other kwargs supported by :class:`JSONVaspInputSet`.
        """
        #NEB specific defaults
        defaults = {'IMAGES': nimages, 'IBRION': 1, 'NFREE': 2, 'ISYM': 0}
        if user_incar_settings:
            defaults.update(user_incar_settings)
        
        with open(os.path.join(MODULE_DIR, "MITVaspInputSet.json")) as f:
            DictVaspInputSet.__init__(self, "MIT NEB", json.load(f),
                                      user_incar_settings=defaults, **kwargs)
        self.nimages = nimages

    def write_input(self, structures, output_dir, make_dir_if_not_present=True,
                    write_cif=False):
        """
        NEB inputs has a special directory structure where inputs are in 00,
        01, 02, ....

        Args:
            structures:
                list of Structure objects. There should be nimages + 2
                structures (including start and end structures).
            output_dir:
                Directory to output the VASP input files
            make_dir_if_not_present:
                Set to True if you want the directory (and the whole path) to
                be created if it is not present.
            write_cif:
                If true, writes a cif along with each POSCAR
        """
        if len(structures) != self.incar_settings['IMAGES'] + 2:
            raise ValueError('incorrect number of structures')
        if make_dir_if_not_present and not os.path.exists(output_dir):
            os.makedirs(output_dir)
        s0 = structures[0]
        self.get_incar(s0).write_file(os.path.join(output_dir, 'INCAR'))
        self.get_kpoints(s0).write_file(os.path.join(output_dir, 'KPOINTS'))
        self.get_potcar(s0).write_file(os.path.join(output_dir, 'POTCAR'))
        for i, s in enumerate(structures):
            d = os.path.join(output_dir, str(i).zfill(2))
            if make_dir_if_not_present and not os.path.exists(d):
                os.makedirs(d)
            self.get_poscar(s).write_file(os.path.join(d, 'POSCAR'))
            if write_cif:
                write_structure(s, os.path.join(d, '{}.cif'.format(i)))

    @property
    def to_dict(self):
        d = super(MITNEBVaspInputSet, self).to_dict
        d["nimages"] = self.nimages
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(user_incar_settings=d.get("user_incar_settings", None),
                   constrain_total_magmom=d["constrain_total_magmom"],
                   sort_structure=d.get("sort_structure", True),
                   hubbard_off=d.get("hubbard_off", False),
                   nimages=d["nimages"])


class MITMDVaspInputSet(DictVaspInputSet):
    """
    Class for writing a vasp md run. This DOES NOT do multiple stage
    runs.
    """

    def __init__(self, start_temp, end_temp, nsteps, time_step=2,
                 hubbard_off=True, spin_polarized=False,
                 sort_structure=False, user_incar_settings=None,
                 **kwargs):
        """
        Args:
            start_temp:
                Starting temperature.
            end_temp:
                Final temperature.
            nsteps:
                Number of time steps for simulations. The NSW parameter.
            time_step:
                The time step for the simulation. The POTIM parameter.
                Defaults to 2fs.
            hubbard_off:
                Whether to turn off Hubbard U. Defaults to *True* (different
                behavior from standard input sets) for MD runs.
            sort_structure:
                Whether to sort structure. Defaults to False (different
                behavior from standard input sets).
            user_incar_settings:
                Settings to override the default incar settings (as a dict)
            **kwargs:
                Other kwargs supported by :class:`DictVaspInputSet`.
        """
        #MD default settings
        defaults = {'TEBEG': start_temp, 'TEEND': end_temp, 'NSW': nsteps,
                    'EDIFF': 0.000001, 'LSCALU': False, 'LCHARG': False,
                    'LPLANE': False, 'LWAVE': True, 'ICHARG': 0, 'ISMEAR': 0,
                    'SIGMA': 0.05, 'NELMIN': 4, 'LREAL': True, 'BMIX': 1,
                    'MAXMIX': 20, 'NELM': 500, 'NSIM': 4, 'ISYM': 0,
                    'ISIF': 0, 'IBRION': 0, 'NBLOCK': 1, 'KBLOCK': 100,
                    'SMASS': 0, 'POTIM': time_step, 'PREC': 'Normal',
                    'ISPIN': 2 if spin_polarized else 1}

        #override default settings with user supplied settings
        if user_incar_settings:
            defaults.update(user_incar_settings)
        with open(os.path.join(MODULE_DIR, "MITVaspInputSet.json")) as f:
            DictVaspInputSet.__init__(
                self, "MIT MD", json.load(f),
                hubbard_off=hubbard_off, sort_structure=sort_structure,
                user_incar_settings=defaults, **kwargs)

        self.start_temp = start_temp
        self.end_temp = end_temp
        self.nsteps = nsteps
        self.time_step = time_step
        self.spin_polarized = spin_polarized
        self.user_incar_settings = user_incar_settings or {}

        #use VASP default ENCUT
        if 'ENCUT' not in self.user_incar_settings:
            del self.incar_settings['ENCUT']

    def get_kpoints(self, structure):
        return Kpoints.gamma_automatic()

    @property
    def to_dict(self):
        d = super(MITMDVaspInputSet, self).to_dict
        d.update({
            "start_temp": self.start_temp,
            "end_temp": self.end_temp,
            "nsteps": self.nsteps,
            "time_step": self.time_step,
            "spin_polarized": self.spin_polarized,
            "user_incar_settings": self.user_incar_settings
        })
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(start_temp=d["start_temp"], end_temp=d["end_temp"],
                   nsteps=d["nsteps"], time_step=d["time_step"],
                   hubbard_off=d.get("hubbard_off", False),
                   user_incar_settings=d["user_incar_settings"],
                   spin_polarized=d.get("spin_polarized", False),
                   constrain_total_magmom=d["constrain_total_magmom"],
                   sort_structure=d.get("sort_structure", True))


MPVaspInputSet = partial(DictVaspInputSet.from_json_file, "MP",
                         os.path.join(MODULE_DIR, "MPVaspInputSet.json"))
"""
Implementation of VaspInputSet utilizing parameters in the public
Materials Project. Typically, the pseudopotentials chosen contain more
electrons than the MIT parameters, and the k-point grid is ~50% more dense.
The LDAUU parameters are also different due to the different psps used,
which result in different fitted values. Supports the same kwargs as
:class:`JSONVaspInputSet`.
"""

MPGGAVaspInputSet = partial(DictVaspInputSet.from_json_file, "MP GGA",
                            os.path.join(MODULE_DIR, "MPVaspInputSet.json"),
                            hubbard_off=True)
"""
Same as the MPVaspInput set, but the +U is enforced to be turned off.
"""


class MPStaticVaspInputSet(DictVaspInputSet):
    """
    Implementation of VaspInputSet overriding MaterialsProjectVaspInputSet
    for static calculations that typically follow relaxation runs.
    It is recommended to use the static from_previous_run method to construct
    the input set to inherit most of the functions.
    """

    def __init__(self, kpoints_density=90, sym_prec=0.01, *args, **kwargs):
        """
        Supports the same kwargs as :class:`JSONVaspInputSet`.
        Args:
            kpoints_density:
                kpoints density for the reciprocal cell of structure.
                Might need to increase the default value when calculating
                metallic materials.
            sym_prec:
                Tolerance for symmetry finding
        """
        with open(os.path.join(MODULE_DIR, "MPVaspInputSet.json")) as f:
            DictVaspInputSet.__init__(
                self, "MP Static", json.load(f), **kwargs)
        self.incar_settings.update(
            {"IBRION": -1, "ISMEAR": -5, "LAECHG": True, "LCHARG": True,
             "LORBIT": 11, "LVHAR": True, "LWAVE": False, "NSW": 0,
             "ICHARG": 0, "EDIFF": 0.000001})
        self.kpoints_settings.update({"kpoints_density": kpoints_density})
        self.sym_prec= sym_prec

    def get_kpoints(self, structure):
        """
        Get a KPOINTS file using the fully automated grid method. Uses
        Gamma centered meshes for hexagonal cells and Monk grids otherwise.
        """
        self.kpoints_settings['grid_density'] = \
            self.kpoints_settings["kpoints_density"] * structure.lattice.reciprocal_lattice.volume * \
            structure.num_sites
        return super(MPStaticVaspInputSet, self).get_kpoints(structure)

    def get_poscar(self, structure):
        sym_finder = SymmetryFinder(structure, symprec=self.sym_prec)
        return Poscar(sym_finder.get_primitive_standard_structure())

    @staticmethod
    def get_structure(vasp_run, outcar=None, initial_structure=False,
                      additional_info=False, sym_prec=0.01):
        """
        Process structure for static calculations from previous run.

        Args:
            vasp_run:
                Vasprun object that contains the final structure from previous
                run.
            outcar:
                Outcar object that contains the magnetization info from
                previous run.
            initial_structure:
                Whether to return the structure from previous run. Default is
                False.
            additional_info:
                Whether to return additional symmetry info related to the
                structure. If True, return a list of the refined structure (
                conventional cell), the conventional standard structure,
                the symmetry dataset and symmetry operations of the structure
                (see SymmetryFinder doc for details)

        Returns:
            Returns the magmom-decorated structure that can be passed to get
            Vasp input files, e.g. get_kpoints.
        """
        if vasp_run.is_spin:
            if outcar and outcar.magnetization:
                magmom = {"magmom": [i['tot'] for i in outcar.magnetization]}
            else:
                magmom = {
                    "magmom": vasp_run.to_dict['input']['parameters']
                    ['MAGMOM']}
        else:
            magmom = None
        structure = vasp_run.final_structure
        if magmom:
            structure = structure.copy(site_properties=magmom)
        sym_finder = SymmetryFinder(structure, symprec=sym_prec)
        if initial_structure:
            return structure
        elif additional_info:
            info = [sym_finder.get_refined_structure(),
                    sym_finder.get_conventional_standard_structure(),
                    sym_finder.get_symmetry_dataset(),
                    sym_finder.get_symmetry_operations()]
            return [sym_finder.get_primitive_standard_structure(),
                    info]
        else:
            return sym_finder.get_primitive_standard_structure()

    @staticmethod
    def from_previous_vasp_run(previous_vasp_dir, output_dir='.',
                               user_incar_settings=None,
                               make_dir_if_not_present=True,
                               kpoints_density=90, sym_prec=0.01):
        """
        Generate a set of Vasp input files for static calculations from a
        directory of previous Vasp run.

        Args:
            previous_vasp_dir:
                The directory contains the outputs(vasprun.xml and OUTCAR) of
                previous vasp run.
            output_dir:
                The directory to write the VASP input files for the static
                calculations. Default to write in the current directory.
            make_dir_if_not_present:
                Set to True if you want the directory (and the whole path) to
                be created if it is not present.
        """
        # Read input and output from previous run
        try:
            vasp_run = Vasprun(os.path.join(previous_vasp_dir, "vasprun.xml"),
                               parse_dos=False, parse_eigen=None)
            outcar = Outcar(os.path.join(previous_vasp_dir, "OUTCAR"))
            previous_incar = vasp_run.incar
            previous_kpoints = vasp_run.kpoints
            previous_final_structure = vasp_run.final_structure
        except:
            traceback.format_exc()
            raise RuntimeError("Can't get valid results from previous run")

        mpsvip = MPStaticVaspInputSet(kpoints_density=kpoints_density, sym_prec=sym_prec)
        structure = mpsvip.get_structure(vasp_run, outcar)

        mpsvip.write_input(structure, output_dir, make_dir_if_not_present)
        new_incar = mpsvip.get_incar(structure)

        # Use previous run INCAR and override necessary parameters
        previous_incar.update({"IBRION": -1, "ISMEAR": -5, "LAECHG": True,
                               "LCHARG": True, "LORBIT": 11, "LVHAR": True,
                               "LWAVE": False, "NSW": 0, "ICHARG": 0})

        for incar_key in ["MAGMOM", "NUPDOWN"]:
            if new_incar.get(incar_key, None):
                previous_incar.update({incar_key: new_incar[incar_key]})
            else:
                previous_incar.pop(incar_key, None)

        # use new LDAUU when possible b/c the Poscar might have changed
        # representation
        if previous_incar.get('LDAU'):
            u = previous_incar.get('LDAUU', [])
            j = previous_incar.get('LDAUJ', [])
            if sum([u[x] - j[x] for x, y in enumerate(u)]) > 0:
                for tag in ('LDAUU', 'LDAUL', 'LDAUJ'):
                    previous_incar.update({tag: new_incar[tag]})

        # Compare ediff between previous and staticinputset values,
        # choose the tighter ediff
        previous_incar.update({"EDIFF": min(previous_incar.get("EDIFF", 1),
                                            new_incar["EDIFF"])})

        # add user settings
        if user_incar_settings:
            previous_incar.update(user_incar_settings)
        previous_incar.write_file(os.path.join(output_dir, "INCAR"))

        # Prefer to use k-point scheme from previous run
        previous_kpoints_density = np.prod(previous_kpoints.kpts[0]) / \
            previous_final_structure.lattice.reciprocal_lattice.volume
        new_kpoints_density = max(previous_kpoints_density, kpoints_density)
        new_kpoints = mpsvip.get_kpoints(structure)
        if previous_kpoints.style[0] != new_kpoints.style[0]:
            if previous_kpoints.style[0] == "M" and \
                    SymmetryFinder(structure, 0.01).get_lattice_type() != \
                    "hexagonal":
                k_div = (kp + 1 if kp % 2 == 1 else kp
                         for kp in new_kpoints.kpts[0])
                Kpoints.monkhorst_automatic(k_div). \
                    write_file(os.path.join(output_dir, "KPOINTS"))
            else:
                Kpoints.gamma_automatic(new_kpoints.kpts[0]). \
                    write_file(os.path.join(output_dir, "KPOINTS"))
        else:
            new_kpoints.write_file(os.path.join(output_dir, "KPOINTS"))


class MPNonSCFVaspInputSet(MPStaticVaspInputSet):
    """
    Implementation of VaspInputSet overriding MaterialsProjectVaspInputSet
    for non self-consistent field (NonSCF) calculation that follows
    a static run to calculate bandstructure, density of states(DOS) and etc.
    It is recommended to use the NonSCF from_previous_run method to construct
    the input set to inherit most of the functions.
    """

    def __init__(self, user_incar_settings, mode="Line",
                 constrain_total_magmom=False, sort_structure=False,
                 kpoints_density=1000, sym_prec=0.01):
        """
        Args:
            user_incar_settings:
                A dict specify customized settings for INCAR.
                Must contain a NBANDS value, suggest to use
                1.2*(NBANDS from static run).
            mode:
                Line: Generate k-points along symmetry lines for bandstructure
                Uniform: Generate uniform k-points grids for DOS
        """
        self.mode = mode
        self.sym_prec= sym_prec
        if mode not in ["Line", "Uniform"]:
            raise ValueError("Supported modes for NonSCF runs are 'Line' and "
                             "'Uniform'!")
        with open(os.path.join(MODULE_DIR, "MPVaspInputSet.json")) as f:
            DictVaspInputSet.__init__(
                self, "MaterialsProject Static", json.load(f),
                constrain_total_magmom=constrain_total_magmom,
                sort_structure=sort_structure)
        self.user_incar_settings = user_incar_settings
        self.incar_settings.update(
            {"IBRION": -1, "ISMEAR": 0, "SIGMA": 0.001, "LCHARG": False,
             "LORBIT": 11, "LWAVE": False, "NSW": 0, "ISYM": 0, "ICHARG": 11})
        self.kpoints_settings.update({"kpoints_density": kpoints_density})
        if mode == "Uniform":
            # Set smaller steps for DOS output
            self.incar_settings.update({"NEDOS": 601})
        if "NBANDS" not in user_incar_settings:
            raise KeyError("For NonSCF runs, NBANDS value from SC runs is "
                           "required!")
        else:
            self.incar_settings.update(user_incar_settings)

    def get_kpoints(self, structure):
        """
        Get a KPOINTS file for NonSCF calculation. In "Line" mode, kpoints are
        generated along high symmetry lines. In "Uniform" mode, kpoints are
        Gamma-centered mesh grid. Kpoints are written explicitly in both cases.

        Args:
            kpoints_density:
                kpoints density for the reciprocal cell of structure.
                Suggest to use a large kpoints_density.
                Might need to increase the default value when calculating
                metallic materials.
        """
        if self.mode == "Line":
            kpath = HighSymmKpath(structure)
            cart_k_points, k_points_labels = kpath.get_kpoints()
            frac_k_points = [kpath._prim_rec.get_fractional_coords(k)
                             for k in cart_k_points]
            return Kpoints(comment="Non SCF run along symmetry lines",
                           style="Reciprocal", num_kpts=len(frac_k_points),
                           kpts=frac_k_points, labels=k_points_labels,
                           kpts_weights=[1] * len(cart_k_points))
        else:
            num_kpoints = self.kpoints_settings["kpoints_density"] * \
                structure.lattice.reciprocal_lattice.volume
            kpoints = Kpoints.automatic_density(
                structure, num_kpoints * structure.num_sites)
            mesh = kpoints.kpts[0]
            ir_kpts = SymmetryFinder(structure, symprec=self.sym_prec) \
                .get_ir_reciprocal_mesh(mesh)
            kpts = []
            weights = []
            for k in ir_kpts:
                kpts.append(k[0])
                weights.append(int(k[1]))
            return Kpoints(comment="Non SCF run on uniform grid",
                           style="Reciprocal", num_kpts=len(ir_kpts),
                           kpts=kpts, kpts_weights=weights)

    @staticmethod
    def get_incar_settings(vasp_run, outcar=None):
        """
        Helper method to get necessary user_incar_settings from previous run.
        """
        # Turn off spin when magmom for every site is smaller than 0.02.
        if outcar and outcar.magnetization:
            site_magmom = np.array([i['tot'] for i in outcar.magnetization])
            ispin = 2 if np.any(site_magmom[np.abs(site_magmom) > 0.02]) else 1
        elif vasp_run.is_spin:
            ispin = 2
        else:
            ispin = 1
        nbands = int(np.ceil(vasp_run.to_dict["input"]["parameters"]["NBANDS"]
                             * 1.2))
        incar_settings = {"ISPIN": ispin, "NBANDS": nbands}
        for grid in ["NGX", "NGY", "NGZ"]:
            if vasp_run.incar.get(grid):
                incar_settings.update({grid:vasp_run.incar.get(grid)})
        return incar_settings

    def get_incar(self, structure):
        incar = super(MPNonSCFVaspInputSet, self).get_incar(structure)
        incar.pop("MAGMOM", None)
        return incar

    @staticmethod
    def from_previous_vasp_run(previous_vasp_dir, output_dir='.',
                               mode="Uniform", user_incar_settings=None,
                               copy_chgcar=True, make_dir_if_not_present=True):
        """
        Generate a set of Vasp input files for NonSCF calculations from a
        directory of previous static Vasp run.

        Args:
            previous_vasp_dir:
                The directory contains the outputs(vasprun.xml and OUTCAR) of
                previous vasp run.
            output_dir:
                The directory to write the VASP input files for the NonSCF
                calculations. Default to write in the current directory.
            copy_chgcar:
                Default to copy CHGCAR from SC run
            make_dir_if_not_present:
                Set to True if you want the directory (and the whole path) to
                be created if it is not present.
        """
        try:
            vasp_run = Vasprun(os.path.join(previous_vasp_dir, "vasprun.xml"),
                               parse_dos=False, parse_eigen=None)
            outcar = Outcar(os.path.join(previous_vasp_dir, "OUTCAR"))
        except:
            traceback.format_exc()
            raise RuntimeError("Can't get valid results from previous run")

        #Get a Magmom-decorated structure
        structure = MPNonSCFVaspInputSet.get_structure(vasp_run, outcar)
        user_incar_settings = MPNonSCFVaspInputSet.get_incar_settings(vasp_run,
                                                                      outcar)
        mpnscfvip = MPNonSCFVaspInputSet(user_incar_settings, mode)
        mpnscfvip.write_input(structure, output_dir, make_dir_if_not_present)
        if copy_chgcar:
            try:
                shutil.copyfile(os.path.join(previous_vasp_dir, "CHGCAR"),
                                os.path.join(output_dir, "CHGCAR"))
            except Exception as e:
                traceback.format_exc()
                raise RuntimeError("Can't copy CHGCAR from SC run" + '\n'
                                   + str(e))


def batch_write_vasp_input(structures, vasp_input_set, output_dir,
                           make_dir_if_not_present=True, subfolder=None,
                           sanitize=False, include_cif=False):
    """
    Batch write vasp input for a sequence of structures to
    output_dir, following the format output_dir/{group}/{formula}_{number}.

    Args:
        structures:
            Sequence of Structures.
        vasp_input_set:
            pymatgen.io.vaspio_set.VaspInputSet like object that creates
            vasp input files from structures
        output_dir:
            Directory to output files
        make_dir_if_not_present:
            Create the directory if not present. Defaults to True.
        subfolder:
            function to create subdirectory name from structure.
            Defaults to simply "formula_count".
        sanitize:
            Boolean indicating whether to sanitize the structure before
            writing the VASP input files. Sanitized output are generally easier
            for viewing and certain forms of analysis. Defaults to False.
        include_cif:
            Boolean indication whether to output a CIF as well. CIF files are
            generally better supported in visualization programs.
    """
    for i, s in enumerate(structures):
        formula = re.sub("\s+", "", s.formula)
        if subfolder is not None:
            subdir = subfolder(s)
            dirname = os.path.join(output_dir, subdir)
        else:
            dirname = os.path.join(output_dir, '{}_{}'.format(formula, i))
        if sanitize:
            s = s.copy(sanitize=True)
        vasp_input_set.write_input(
            s, dirname, make_dir_if_not_present=make_dir_if_not_present
        )
        if include_cif:
            writer = CifWriter(s)
            writer.write_file(os.path.join(
                dirname, "{}_{}.cif".format(formula, i)))
