import os
import platform
import sys
import warnings
from setuptools import setup, find_packages, Extension
from setuptools.command.build_ext import build_ext
from distutils.sysconfig import customize_compiler


class custom_build_ext(build_ext):
    """Allow C extension building to fail.
    The C extension speeds up word2vec and doc2vec training, but is not essential.
    """

    warning_message = """
    ********************************************************************
    WARNING: %s could not
    be compiled. No C extensions are essential for fieldembed to run,
    although they do result in significant speed improvements for some modules.
    %s
    Here are some hints for popular operating systems:
    If you are seeing this message on Linux you probably need to
    install GCC and/or the Python development package for your
    version of Python.
    Debian and Ubuntu users should issue the following command:
        $ sudo apt-get install build-essential python-dev
    RedHat, CentOS, and Fedora users should issue the following command:
        $ sudo yum install gcc python-devel
    If you are seeing this message on OSX please read the documentation
    here:
    http://api.mongodb.org/python/current/installation.html#osx
    ********************************************************************
    """

    def run(self):
        try:
            build_ext.run(self)
        except Exception:
            e = sys.exc_info()[1]
            sys.stdout.write('%s\n' % str(e))
            warnings.warn(
                self.warning_message +
                "Extension modules" +
                "There was an issue with your platform configuration - see above.")

    def build_extension(self, ext):
        name = ext.name
        customize_compiler(self.compiler)
        try:
            self.compiler.compiler_so.remove("-Wstrict-prototypes") # add this.
            build_ext.build_extension(self, ext)
        except Exception:
            e = sys.exc_info()[1]
            sys.stdout.write('%s\n' % str(e))
            warnings.warn(
                self.warning_message +
                "The %s extension module" % (name,) +
                "The output above this warning shows how the compilation failed.")

    # the following is needed to be able to add numpy's include dirs... without
    # importing numpy directly in this script, before it's actually installed!
    # http://stackoverflow.com/questions/19919905/how-to-bootstrap-numpy-installation-in-setup-py
    def finalize_options(self):
        build_ext.finalize_options(self)
        # Prevent numpy from thinking it is still in its setup process:
        # https://docs.python.org/2/library/__builtin__.html#module-__builtin__
        if isinstance(__builtins__, dict):
            __builtins__["__NUMPY_SETUP__"] = False
        else:
            __builtins__.__NUMPY_SETUP__ = False

        import numpy
        self.include_dirs.append(numpy.get_include())


model_dir = os.path.join(os.path.dirname(__file__), 'fieldembed')
# fieldembed_dir = os.path.join(os.path.dirname(__file__), 'fieldembed')

cmdclass = {'build_ext': custom_build_ext}

WHEELHOUSE_UPLOADER_COMMANDS = {'fetch_artifacts', 'upload_all'}
if WHEELHOUSE_UPLOADER_COMMANDS.intersection(sys.argv):
    import wheelhouse_uploader.cmd
    cmdclass.update(vars(wheelhouse_uploader.cmd))


distributed_env = ['Pyro4 >= 4.27']

win_testenv = [
    'pytest',
    'pytest-rerunfailures',
    'mock',
    'cython',
    'pyemd',
    'testfixtures',
    'scikit-learn',
    'Morfessor==2.0.2a4',
    'python-Levenshtein >= 0.10.2',
    'visdom >= 0.1.8, != 0.1.8.7',
]

linux_testenv = win_testenv[:]

ext_modules = [
    # Extension('fieldembed.models.word2vec_inner',
    #     sources=['./fieldembed/models/word2vec_inner.c'],
    #     include_dirs=[model_dir]),

    # Extension('fieldembed.models.fieldembed_inner',
    #     sources=['./fieldembed/models/fieldembed_inner.c'],
    #     include_dirs=[model_dir]),

    Extension('fieldembed._matutils',
        sources=['./fieldembed/_matutils.c'],
        include_dirs=[model_dir],
         ),
]


if not (os.name == 'nt' and sys.version_info[0] < 3):
    extra_args = []
    system = platform.system()

    if system == 'Linux':
        extra_args.append('-std=c++11')
    elif system == 'Darwin':
        extra_args.extend(['-stdlib=libc++', '-std=c++11'])

    ext_modules.append(
        Extension('fieldembed.fieldembed_core',
            sources=['./fieldembed/fieldembed_core.cpp'],
            language='c++',
            include_dirs=[model_dir],
            extra_compile_args=extra_args,
            extra_link_args=extra_args)
    )


setup(
    ext_modules=ext_modules,
    cmdclass=cmdclass,
    
    setup_requires=[
        'numpy >= 1.11.3'
    ],
    install_requires=[
        'numpy >= 1.11.3',
        'scipy >= 0.18.1',
        'six >= 1.5.0',
        'smart_open >= 1.7.0',
    ],
    extras_require={
        'distributed': distributed_env,
        'test-win': win_testenv,
        'test': linux_testenv,
        'docs': linux_testenv + distributed_env + ['sphinx', 'sphinxcontrib-napoleon', 'plotly', 'pattern <= 2.6', 'sphinxcontrib.programoutput'],
    },

    include_package_data=True,
)