from copy import deepcopy
import errno
from datetime import datetime
import os
import subprocess
import sys

import pytz
import ray
from ray.rllib.agents.ppo.ppo_policy import PPOTFPolicy
from ray.rllib.agents.ppo.ppo import PPOTrainer, DEFAULT_CONFIG as DEFAULT_PPO_CONFIG
from ray.rllib.agents.sac.sac_policy import SACTFPolicy
from ray.rllib.agents.sac.sac import SACTrainer, DEFAULT_CONFIG as DEFAULT_SAC_CONFIG

from ray.rllib.agents.ddpg.td3 import TD3Trainer, TD3_DEFAULT_CONFIG as DEFAULT_TD3_CONFIG

from ray.rllib.models import ModelCatalog
from ray import tune
from ray.tune import Trainable
from ray.tune.logger import pretty_print
from ray.tune import run as run_tune
from ray.tune.registry import register_env


from envs.lerrel.adv_hopper import AdvMAHopper
from envs.lerrel.adv_inverted_pendulum_env import AdvMAPendulumEnv
from visualize.pendulum.transfer_tests import run_transfer_tests
# from visualize.pendulum.visualize_adversaries import visualize_adversaries
from utils.pendulum_env_creator import make_create_env
from utils.parsers import init_parser, ray_parser, ma_env_parser
from utils.rllib_utils import get_config_from_path

from models.recurrent_tf_model_v2 import LSTM

def setup_ma_config(config, create_env):
    env = create_env(config['env_config'])
    policies_to_train = ['agent']

    num_adversaries = config['env_config']['num_adv_strengths'] * config['env_config']['advs_per_strength']
    if num_adversaries == 0:
        return
    adv_policies = ['adversary' + str(i) for i in range(num_adversaries)]
    adversary_config = {"model": {'fcnet_hiddens': [32, 32], 'use_lstm': False}}
    policy_graphs = {'agent': (PPOTFPolicy, env.observation_space, env.action_space, {})}
    policy_graphs.update({adv_policies[i]: (PPOTFPolicy, env.adv_observation_space,
                                            env.adv_action_space, adversary_config) for i in range(num_adversaries)})
    
    # TODO(@evinitsky) put this back
    # policy_graphs.update({adv_policies[i]: (CustomPPOPolicy, env.adv_observation_space,
    #                                         env.adv_action_space, adversary_config) for i in range(num_adversaries)})

    print("========= Policy Graphs ==========")
    print(policy_graphs)

    policies_to_train += adv_policies

    def policy_mapping_fn(agent_id):
        return agent_id

    config.update({
        'multiagent': {
            'policies': policy_graphs,
            'policy_mapping_fn': policy_mapping_fn,
            'policies_to_train': policies_to_train
        }
    })
    print({'multiagent': {
            'policies': policy_graphs,
            'policy_mapping_fn': policy_mapping_fn,
            'policies_to_train': policies_to_train
        }})


def setup_exps(args):
    parser = init_parser()
    parser = ray_parser(parser)
    parser = ma_env_parser(parser)
    parser.add_argument('--env_name', default='pendulum', const='pendulum', nargs='?', choices=['pendulum', 'hopper'])
    parser.add_argument('--algorithm', default='PPO', type=str, help='Options are PPO, SAC, TD3')
    parser.add_argument('--custom_ppo', action='store_true', default=False, help='If true, we use the PPO with a KL penalty')
    parser.add_argument('--num_adv_strengths', type=int, default=1, help='Number of adversary strength ranges. '
                                                                         'Multiply this by `advs_per_strength` to get the total number of adversaries'
                                                                         'Default - retrain lerrel, single agent')
    parser.add_argument('--advs_per_strength', type=int, default=1, help='How many adversaries exist at each strength level')
    parser.add_argument('--adv_strength', type=float, default=5.0, help='Strength of active adversaries in the env')
    parser.add_argument('--alternate_training', action='store_true', default=False)
    parser.add_argument('--curriculum', action='store_true', default=False,
                        help='If true, the number of adversaries is increased every `adv_incr_freq` steps that'
                             'we are above goal score')
    parser.add_argument('--goal_score', type=float, default=3000.0,
                        help='This is the score we need to maintain for `adv_incr_freq steps before the number'
                             'of adversaries increase')
    parser.add_argument('--adv_incr_freq', type=int, default=20,
                        help='If you stay above `goal_score` for this many steps, the number of adversaries'
                             'will increase')
    parser.add_argument('--num_concat_states', type=int, default=1,
                        help='This number sets how many previous states we concatenate into the observations')
    parser.add_argument('--concat_actions', action='store_true', default=False,
                        help='If true we concatenate prior actions into the state. This helps a lot for prediction.')
    args = parser.parse_args(args)

    if args.alternate_training and args.num_adv > 1:
        sys.exit('You can only have 1 adversary if you are alternating training')

    alg_run = args.algorithm

    # Universal hyperparams
    if args.algorithm == 'PPO':
        config = DEFAULT_PPO_CONFIG
        config['train_batch_size'] = args.train_batch_size
        if args.grid_search:
            config['lambda'] = tune.grid_search([0.85, 0.9, 0.95])
            config['lr'] = tune.grid_search([1e-4, 5e-4])
            config['vf_clip_param'] = 100.0
        else:
            if args.env_name == 'hopper':
                config['lambda'] = 0.97
                config['lr'] = 1e-2
                config['vf_clip_param'] = 100.0
            else:
                config['lambda'] = 0.9
                config['lr'] = 5e-5
        config['sgd_minibatch_size'] = 64
        config['num_sgd_iter'] = 10
        config['observation_filter'] = 'NoFilter'
    elif args.algorithm == 'SAC':
        config = DEFAULT_SAC_CONFIG
        config['target_network_update_freq'] = 1
    elif args.algorithm == 'TD3':
        config = DEFAULT_TD3_CONFIG
        # === Exploration ===
        config['learning_starts'] = 10000
        config['pure_exploration_steps'] = 10000
        # === Evaluation ===
        config['evaluation_interval'] = 5
        config['evaluation_num_episodes'] = 10
    else:
        sys.exit('Only PPO, TD3, and SAC are supported')

    config['num_workers'] = args.num_cpus
    config['gamma'] = 0.995
    # config["batch_mode"] = "complete_episodes"
    config['seed'] = 0

    # config['num_adversaries'] = args.num_adv
    # config['kl_diff_weight'] = args.kl_diff_weight
    # config['kl_diff_target'] = args.kl_diff_target
    # config['kl_diff_clip'] = 5.0

    config['env_config']['num_adv_strengths'] = args.num_adv_strengths
    config['env_config']['advs_per_strength'] = args.advs_per_strength
    config['env_config']['adversary_strength'] = args.adv_strength
    config['env_config']['curriculum'] = args.curriculum
    config['env_config']['goal_score'] = args.goal_score
    config['env_config']['adv_incr_freq'] = args.adv_incr_freq
    config['env_config']['concat_actions'] = args.concat_actions
    config['env_config']['num_concat_states'] = args.num_concat_states

    config['env_config']['run'] = alg_run

    ModelCatalog.register_custom_model("rnn", LSTM)
    config['model']['fcnet_hiddens'] = [64, 64]
    # TODO(@evinitsky) turn this on
    config['model']['use_lstm'] = False
    # config['model']['custom_model'] = "rnn"
    config['model']['lstm_use_prev_action_reward'] = False
    config['model']['lstm_cell_size'] = 128
    if args.grid_search:
        config['vf_loss_coeff'] = tune.grid_search([1e-4, 1e-3])

    if args.env_name == "pendulum":
        env_name = "MALerrelPendulumEnv"
        env_tag = "pendulum"
        create_env_fn = make_create_env(AdvMAPendulumEnv)
    elif args.env_name == "hopper":
        env_name = "MALerrelHopperEnv"
        env_tag = "hopper"
        create_env_fn = make_create_env(AdvMAHopper)

    config['env'] = env_name
    register_env(env_name, create_env_fn)

    setup_ma_config(config, create_env_fn)

    # add the callbacks
    config["callbacks"] = {"on_train_result": on_train_result,
                           "on_episode_end": on_episode_end}

    # config["eager_tracing"] = True
    # config["eager"] = True
    # config["eager_tracing"] = True

    # create a custom string that makes looking at the experiment names easier
    def trial_str_creator(trial):
        return "{}_{}".format(trial.trainable_name, trial.experiment_tag)

    exp_dict = {
        'name': args.exp_title,
        # 'run_or_experiment': KLPPOTrainer,
        'run_or_experiment': args.algorithm,
        'trial_name_creator': trial_str_creator,
        'checkpoint_freq': args.checkpoint_freq,
        'stop': {
            'training_iteration': args.num_iters
        },
        'config': config,
        'num_samples': args.num_samples,
    }
    return exp_dict, args


def on_train_result(info):
    """Store the mean score of the episode, and increment or decrement how many adversaries are on"""
    result = info["result"]

    if 'policy_reward_mean' in result.keys():
        if 'agent' in result['policy_reward_mean'].keys():
            pendulum_reward = result['policy_reward_mean']['agent']
            trainer = info["trainer"]

            trainer.workers.foreach_worker(
                lambda ev: ev.foreach_env(
                    lambda env: env.update_curriculum(pendulum_reward)))


def on_episode_end(info):
    """Select the currently active adversary"""

    # store info about how many adversaries there are
    if hasattr(info["env"], 'envs'):
        env = info["env"].envs[0]
        env.select_new_adversary()

        episode = info["episode"]
        episode.custom_metrics["num_active_advs"] = env.adversary_range


class AlternateTraining(Trainable):
    def _setup(self, config):
        self.config = config
        self.env = config['env']
        agent_config = self.config
        adv_config = deepcopy(self.config)
        agent_config['multiagent']['policies_to_train'] = ['agent']
        adv_config['multiagent']['policies_to_train'] = ['adversary0']

        self.agent_trainer = PPOTrainer(env=self.env, config=agent_config)
        self.adv_trainer = PPOTrainer(env=self.env, config=adv_config)

    def _train(self):
        # improve the Adversary policy
        print("-- Adversary Training --")
        print(pretty_print(self.adv_trainer.train()))

        # swap weights to synchronize
        self.agent_trainer.set_weights(self.adv_trainer.get_weights(["adversary0"]))

        # improve the Agent policy
        print("-- Agent Training --")
        output = self.agent_trainer.train()
        print(pretty_print(output))

        # swap weights to synchronize
        self.adv_trainer.set_weights(self.agent_trainer.get_weights(["agent"]))
        return output

    def _save(self, tmp_checkpoint_dir):
        return self.agent_trainer._save(tmp_checkpoint_dir)


if __name__ == "__main__":

    exp_dict, args = setup_exps(sys.argv[1:])

    date = datetime.now(tz=pytz.utc)
    date = date.astimezone(pytz.timezone('US/Pacific')).strftime("%m-%d-%Y")
    s3_string = 's3://sim2real/adv_robust/' \
                + date + '/' + args.exp_title
    if args.use_s3:
        exp_dict['upload_dir'] = s3_string

    if args.multi_node:
        ray.init(redis_address='localhost:6379')
    elif args.local_mode:
        ray.init(local_mode=True)
    else:
        ray.init()

    if args.alternate_training:
        exp_dict['run_or_experiment'] = AlternateTraining
    run_tune(**exp_dict, queue_trials=False, raise_on_failed_trial=False)

    # Now we add code to loop through the results and create scores of the results
    if args.run_transfer_tests:
        ray.shutdown()
        ray.init()
        output_path = os.path.join(os.path.join(os.path.expanduser('~/transfer_results/adv_robust'), date), args.exp_title)
        if not os.path.exists(output_path):
            try:
                os.makedirs(output_path)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
        for (dirpath, dirnames, filenames) in os.walk(os.path.expanduser("~/ray_results")):
            if "checkpoint_{}".format(args.num_iters) in dirpath:
                # grab the experiment name
                folder = os.path.dirname(dirpath)
                tune_name = folder.split("/")[-1]
                outer_folder = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                script_path = os.path.expanduser(os.path.join(outer_folder, "visualize/transfer_test.py"))
                config, checkpoint_path = get_config_from_path(folder, str(args.num_iters))

                # TODO(@ev) gross find somewhere else to put this

                if config['env'] == "MALerrelPendulumEnv":
                    from visualize.pendulum.transfer_tests import pendulum_run_list
                    lerrel_run_list = pendulum_run_list
                elif config['env'] == "MALerrelHopperEnv":
                    from visualize.pendulum.transfer_tests import hopper_run_list
                    lerrel_run_list = hopper_run_list

                run_transfer_tests(config, checkpoint_path, 100, args.exp_title, output_path, run_list=lerrel_run_list)

                if args.use_s3:
                    # visualize_adversaries(config, checkpoint_path, 10, 100, output_path)
                    p1 = subprocess.Popen("aws s3 sync {} {}".format(output_path,
                                                                     "s3://sim2real/transfer_results/adv_robust/{}/{}/{}".format(date,
                                                                                                                      args.exp_title,
                                                                                                                      tune_name)).split(
                        ' '))
                    p1.wait()