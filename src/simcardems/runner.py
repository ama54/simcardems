import typing
from pathlib import Path

from tqdm import tqdm

from . import save_load_functions as io
from . import utils
from .config import Config
from .models import em_model
from .time_stepper import TimeStepper


logger = utils.getLogger(__name__)


class Runner:
    def __init__(
        self,
        config: typing.Optional[Config] = None,
        empty: bool = False,
        **kwargs,
    ) -> None:

        if config is None:
            config = Config()

        self._config = config
        self.outdir.mkdir(exist_ok=True)

        from . import set_log_level

        set_log_level(config.loglevel)

        if empty:
            return

        reset = not self._config.load_state
        if not reset and self.state_path.is_file():
            # Load state
            logger.info("Load previously saved state")
            self.coupling = io.load_state(
                path=self.state_path,
                drug_factors_file=self._config.drug_factors_file,
                popu_factors_file=self._config.popu_factors_file,
                disease_state=self._config.disease_state,
                PCL=self._config.PCL,  # Set bcl from cli
            )
        else:
            logger.info("Create a new state")
            # Create a new state
            self.coupling = em_model.setup_EM_model_from_config(self._config)

        self._t0 = self.coupling.t
        self._reset = reset

        self._setup_assigners()
        self._setup_datacollector()

        logger.info(f"Starting at t0={self._t0}")

    @property
    def _dt(self):
        return self._config.dt

    @_dt.setter
    def _dt(self, value):
        self._config.dt = value

    @property
    def state_path(self) -> Path:
        return self.outdir / "state.h5"

    @property
    def outdir(self) -> Path:
        return Path(self._config.outdir)

    @property
    def t(self) -> float:
        if self._time_stepper is None:
            raise RuntimeError("Please create a time stepper before solving")
        return self._time_stepper.t

    @property
    def t0(self) -> float:
        return self._t0

    def _setup_time_stepper(
        self,
        T: float,
        use_ns: bool = True,
        st_progress: typing.Any = None,
    ) -> None:
        self._time_stepper = TimeStepper(
            t0=self._t0,
            T=T,
            dt=self._dt,
            use_ns=use_ns,
            st_progress=st_progress,
        )
        self.coupling.register_time_stepper(self._time_stepper)

    @classmethod
    def from_models(
        cls,
        coupling: em_model.BaseEMCoupling,
        config: typing.Optional[Config] = None,
        reset: bool = True,
    ):
        obj = cls(empty=True, config=config)
        obj.coupling = coupling
        obj._t0 = coupling.t
        obj._reset = reset
        obj._setup_assigners()
        obj._setup_datacollector()
        return obj

    def _setup_assigners(self):
        self._time_stepper = None
        self.coupling.setup_assigners()

    def store(self):
        # Assign u, v and Ca for postprocessing
        self.coupling.assigners.assign()
        self.collector.store(TimeStepper.ns2ms(self.t))

    def _setup_datacollector(self):
        from .datacollector import DataCollector

        self.collector = DataCollector(
            outdir=self.outdir,
            geo=self.coupling.geometry,
            reset_state=self._reset,
        )
        self.coupling.register_datacollector(self.collector)

    def _solve_mechanics_now(self) -> bool:

        # Update these states that are needed in the Mechanics solver
        self.coupling.ep_to_coupling()
        self._pre_mechanics_solve()
        norm = self.coupling.assigners.compute_pre_norm()
        return norm >= 0.05

    def _pre_mechanics_solve(self) -> None:
        self.coupling.assigners.assign_pre()
        self.coupling.coupling_to_mechanics()

    def _post_mechanics_solve(self) -> None:

        # Update previous lmbda
        self.coupling.update_prev_mechanics()
        self.coupling.mechanics_to_coupling()
        self.coupling.coupling_to_ep()

    def _solve_mechanics(self):
        # self._pre_mechanics_solve()
        # if self._config.mechanics_use_continuation:
        #     self.mech_heart.solve_for_control(self.coupling.XS_ep)
        # else:
        self.coupling.solve_mechanics()
        self._post_mechanics_solve()

    def _post_ep(self):
        self.coupling.update_prev_ep()
        self.coupling.ep_to_coupling()

    def save_state(self):
        self.coupling.save_state(path=self.state_path, config=self._config)

    def solve(
        self,
        T: float = Config.T,
        save_freq: int = Config.save_freq,
        show_progress_bar: bool = Config.show_progress_bar,
        st_progress: typing.Any = None,
    ):

        save_it = int(save_freq / self._dt)
        self._setup_time_stepper(T, use_ns=True, st_progress=st_progress)
        pbar = create_progressbar(
            time_stepper=self._time_stepper,
            show_progress_bar=show_progress_bar,
        )

        for (i, (t0, t)) in enumerate(pbar):
            logger.debug(
                f"Solve EP model at step {i} from {TimeStepper.ns2ms(t0):.2f} ms to {TimeStepper.ns2ms(t):.2f} ms",
            )

            # Solve EP model
            self.coupling.t = TimeStepper.ns2ms(t)
            self.coupling.solve_ep((TimeStepper.ns2ms(t0), TimeStepper.ns2ms(t)))
            self._post_ep()

            if self._solve_mechanics_now():
                logger.debug(f"Solve mechanics model at step {i} from ")
                self._solve_mechanics()

            # Store every 'save_freq' ms
            if i % save_it == 0:
                self.store()

        self.save_state()


def create_progressbar(
    time_stepper: TimeStepper,
    show_progress_bar: bool = Config.show_progress_bar,
):
    if show_progress_bar:
        # Show progressbar
        pbar = tqdm(time_stepper, total=time_stepper.total_steps)
    else:
        # Hide progressbar
        pbar = _tqdm(time_stepper, total=time_stepper.total_steps)
    return pbar


class _tqdm:
    def __init__(self, iterable, *args, **kwargs):
        self._iterable = iterable

    def set_postfix(self, msg):
        pass

    def __iter__(self):
        return iter(self._iterable)