import json
import logging
import os
import re
import traceback
from typing import Any, Dict, Optional
import yaml

from dataclasses import dataclass
from getpass import getuser
from pathlib import Path
from rich.logging import RichHandler
from simple_parsing import parse
from simple_parsing.helpers.serialization.serializable import FrozenSerializable
from simple_parsing.helpers.flatten import FlattenedAccess
from sweagent import (
    Agent,
    AgentArguments,
    EnvironmentArguments,
    ModelArguments,
    SWEEnv,
    get_data_path_name,
)
from swebench import KEY_INSTANCE_ID, KEY_MODEL, KEY_PREDICTION
from unidiff import PatchSet

from sweagent.environment.utils import InvalidGithubURL, get_associated_commit_urls, get_gh_issue_data, parse_gh_issue_url

handler = RichHandler(show_time=False, show_path=False)
handler.setLevel(logging.DEBUG)
logger = logging.getLogger("run_dev")
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)
logger.propagate = False
logging.getLogger("simple_parsing").setLevel(logging.WARNING)


@dataclass(frozen=True)
class ActionsArguments(FlattenedAccess, FrozenSerializable):
    """Run real-life actions (opening PRs, etc.) if we can solve the issue."""
    open_pr: bool = False  # Open a PR with the patch if we can solve the issue
    # Skip action if there are already commits claiming to fix the issue. Please only
    # set this to False if you are sure the commits are not fixes or if this is your
    # own repository!
    skip_if_commits_reference_issue: bool = True  
    # For PRs: If you want to push the branch to a fork (e.g., because you lack
    # permissions to push to the main repo), set this to the URL of the fork.
    push_gh_repo_url: str = ""

    def __post_init__(self):
        if not self.skip_if_commits_reference_issue and self.push_gh_repo_url:
            raise ValueError(
                "Overriding `skip_if_commits_reference_issue` when you are "
                "pushing to a fork is not supported. You should manually "
                "apply the patch to the forked repository."
            )

@dataclass(frozen=True)
class ScriptArguments(FlattenedAccess, FrozenSerializable):
    """Configure the control flow of the run.py script"""
    environment: EnvironmentArguments
    agent: AgentArguments
    actions: ActionsArguments
    instance_filter: str = ".*"  # Only run instances that completely match this regex
    skip_existing: bool = True  # Skip instances with existing trajectories
    suffix: str = ""
    # Raise unhandled exceptions during the run (useful for debugging)
    raise_exceptions: bool = False

    @property
    def run_name(self):
        """Generate a unique name for this run based on the arguments."""
        model_name = self.agent.model.model_name.replace(":", "-")
        data_stem = get_data_path_name(self.environment.data_path)
        config_stem = Path(self.agent.config_file).stem

        temp = self.agent.model.temperature
        top_p = self.agent.model.top_p

        per_instance_cost_limit = self.agent.model.per_instance_cost_limit
        install_env = self.environment.install_environment

        return (
            f"{model_name}__{data_stem}__{config_stem}__t-{temp:.2f}__p-{top_p:.2f}"
            + f"__c-{per_instance_cost_limit:.2f}__install-{int(install_env)}"
            + (f"__{self.suffix}" if self.suffix else "")
        )


def main(args: ScriptArguments):
    logger.info(f"📙 Arguments: {args.dumps_yaml()}")
    agent = Agent("primary", args.agent)

    env = SWEEnv(args.environment)

    traj_dir = Path("trajectories") / Path(getuser()) / args.run_name
    traj_dir.mkdir(parents=True, exist_ok=True)

    save_arguments(traj_dir, args)

    for index in range(len(env.data)):
        try:
            # Reset environment
            instance_id = env.data[index]["instance_id"]
            assert isinstance(instance_id, str)  # mypy
            if should_skip(args, traj_dir, instance_id):
                continue
            logger.info("▶️  Beginning task " + str(index))

            observation, info = env.reset(index)
            if info is None:
                continue

            # Get info, patch information
            issue = getattr(env, "query", None)
            files = []
            if "patch" in env.record:
                files = "\n".join(
                    [f"- {x.path}" for x in PatchSet(env.record["patch"]).modified_files]
                )
            # Get test files, F2P tests information
            test_files = []
            if "test_patch" in env.record:
                test_patch_obj = PatchSet(env.record["test_patch"])
                test_files = "\n".join(
                    [f"- {x.path}" for x in test_patch_obj.modified_files + test_patch_obj.added_files]
                )
            tests = ""
            if "FAIL_TO_PASS" in env.record:
                tests = "\n".join([f"- {x}" for x in env.record["FAIL_TO_PASS"]])

            setup_args = {
                "issue": issue,
                "files": files,
                "test_files": test_files,
                "tests": tests
            }
            info, trajectory = agent.run(
                setup_args=setup_args,
                env=env,
                observation=observation,
                traj_dir=traj_dir,
                return_type="info_trajectory",
            )
            save_predictions(traj_dir, instance_id, info)
            save_patch(traj_dir, instance_id, info)
            if args.actions.open_pr and should_open_pr(args, info, token=env._github_token):
                env.open_pr(trajectory=trajectory, push_gh_repo_url=args.actions.push_gh_repo_url)

        except KeyboardInterrupt:
            logger.info("Exiting InterCode environment...")
            env.close()
            break
        except Exception as e:
            traceback.print_exc()
            logger.warning(f"❌ Failed on {env.record['instance_id']}: {e}")
            if args.raise_exceptions:
                raise e
            env.reset_container()
            continue


def should_open_pr(args: ScriptArguments, info: Dict[str, Any], *, token: str="") -> bool:
    """Does opening a PR make sense?"""
    if not info.get("submission"):
        logger.info("Not openening PR because submission was made.")
        return False
    if info["exit_status"] != "submitted":
        logger.info("Not openening PR because exit status was %s and not submitted.", info["exit_status"])
        return False
    try:
        issue = get_gh_issue_data(args.environment.data_path, token=token)
    except InvalidGithubURL:
        logger.info("Currently only github is supported to open PRs to. Skipping PR creation.")
        return False
    if issue.state != "open":
        logger.info(f"Issue is not open (state={issue.state}. Skipping PR creation.")
        return False
    if issue.locked:
        logger.info("Issue is locked. Skipping PR creation.")
        return False
    org, repo, issue_number = parse_gh_issue_url(args.environment.data_path)
    associated_commits = get_associated_commit_urls(org, repo, issue_number, token=token) 
    if associated_commits:
        commit_url_strs = ", ".join(associated_commits)
        if args.actions.skip_if_commits_reference_issue:
            logger.info(f"Issue already has associated commits (see {commit_url_strs}). Skipping PR creation.")
            return False
        else:
            logger.warning(
                "Proceeding with PR creation even though there are already commits "
                f"({commit_url_strs}) associated with the issue. Please only do this for your own repositories "
                "or after verifying that the existing commits do not fix the issue."
            )
    return True


def save_arguments(traj_dir: Path, args: ScriptArguments) -> None:
    """Save the arguments to a yaml file to the run's trajectory directory."""
    log_path = traj_dir / "args.yaml"

    if log_path.exists():
        try:
            other_args = args.load_yaml(log_path)
            if (args.dumps_yaml() != other_args.dumps_yaml()):  # check yaml equality instead of object equality
                logger.warning("**************************************************")
                logger.warning("Found existing args.yaml with different arguments!")
                logger.warning("**************************************************")
        except Exception as e:
            logger.warning(f"Failed to load existing args.yaml: {e}")

    with log_path.open("w") as f:
        args.dump_yaml(f)


def should_skip(args: ScriptArguments, traj_dir: Path, instance_id: str) -> bool:
    """Check if we should skip this instance based on the instance filter and skip_existing flag."""
    # Skip instances that don't match the instance filter
    if re.match(args.instance_filter, instance_id) is None:
        logger.info(f"Instance filter not matched. Skipping instance {instance_id}")
        return True

    # If flag is set to False, don't skip
    if not args.skip_existing:
        return False

    # Check if there's an existing trajectory for this instance
    log_path = traj_dir / (instance_id + ".traj")
    if log_path.exists():
        with log_path.open("r") as f:
            data = json.load(f)
        # If the trajectory has no exit status, it's incomplete and we will redo it
        exit_status = data["info"].get("exit_status", None)
        if exit_status == "early_exit" or exit_status is None:
            logger.info(f"Found existing trajectory with no exit status: {log_path}")
            logger.info("Removing incomplete trajectory...")
            os.remove(log_path)
        else:
            logger.info(f"⏭️ Skipping existing trajectory: {log_path}")
            return True
    return False


def save_predictions(traj_dir: Path, instance_id: str, info):
    output_file = traj_dir / "all_preds.jsonl"
    model_patch = info["submission"] if "submission" in info else None
    datum = {
        KEY_MODEL: Path(traj_dir).name,
        KEY_INSTANCE_ID: instance_id,
        KEY_PREDICTION: model_patch,
    }
    with open(output_file, "a+") as fp:
        print(json.dumps(datum), file=fp, flush=True)
    logger.info(f"Saved predictions to {output_file}")


def save_patch(traj_dir: Path, instance_id: str, info) -> Optional[Path]:
    """Create patch files that can be applied with `git am`.
    
    Returns:
        The path to the patch file, if it was saved. Otherwise, returns None.
    """
    patch_output_dir = traj_dir / "patches"
    patch_output_dir.mkdir(exist_ok=True, parents=True)
    patch_output_file = patch_output_dir / f"{instance_id}.patch"
    if not "submission" in info:
        logger.info("No patch to save.")
        return
    model_patch = info["submission"]
    patch_output_file.write_text(model_patch)
    logger.info(f"Saved patch to {patch_output_file}")
    return patch_output_file


def get_args(args=None) -> ScriptArguments:
    """Parse command line arguments and return a ScriptArguments object.
    
    Args:
        args: Optional list of arguments to parse. If not provided, uses sys.argv.
    """
    defaults = ScriptArguments(
        suffix="",
        environment=EnvironmentArguments(
            image_name="sweagent/swe-agent:latest",
            data_path="princeton-nlp/SWE-bench_Lite",
            split="dev",
            verbose=True,
            install_environment=True,
        ),
        skip_existing=True,
        agent=AgentArguments(
            model=ModelArguments(
                model_name="gpt4",
                total_cost_limit=0.0,
                per_instance_cost_limit=3.0,
                temperature=0.0,
                top_p=0.95,
            ),
            config_file="config/default.yaml",
        ),
        actions=ActionsArguments(open_pr=False, skip_if_commits_reference_issue=True),
    )

    # Nicer yaml dumping of multiline strings
    def multiline_representer(dumper, data):
        """configures yaml for dumping multiline strings
        Ref: https://stackoverflow.com/questions/8640959/how-can-i-control-what-scalar-form-pyyaml-uses-for-my-data
        """
        if data.count("\n") > 0:  # check for multiline string
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    yaml.add_representer(str, multiline_representer)

    return parse(ScriptArguments, default=defaults, add_config_path_arg=False, args=args)


if __name__ == "__main__":
    args = get_args()
    main(args)
