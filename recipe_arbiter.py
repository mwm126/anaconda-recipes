#!/usr/bin/env python



"""
IMPORTANT: This script is for Continuum internal use only.


recipe_arbiter.py - Arbitration for conda package recipes among the ContinuumIO and AnacondaRecipes GitHub organizations

Usage:
- Offers four "actions":

    - diff: View all public recipes that exist in either of the two
      organizations. Also show whether the contents are equal between the
      internal and external versions.

    - externalize: Takes the latest "master" of the ContinuumIO public recipes,
      pushes each one onto a separate branch on their respective AnacondaRecipes
      repos, and opens a PR for each one

    - internalize: Takes the latest "master" branches of the AnacondaRecipes
      repositories, and opens a PR on the ContinuumIO/anaconda repository

Implementation Details:
- Uses the GitHub API, ContinuumIO/anaconda repo, ContinuumIO/anaconda-recipes
  repo, and recipes under the AnacondaRecipes GitHub organization

- Requires GitHub username and password for Basic Authentication, but does not
  store them anywhere

Future improvements:
- We could remove the dependency on ContinuumIO/anaconda-recipes if there were
an alternative way to distinguish between public and private recipes

Tips:
- While developing, the -d/--debug feature is a big time-saver on repeat runs,
especially if the internet is slow.

TODOs:
- Handle the "truncated" case (grep for 'assert .*truncated')
- Add diff feature to actually take diffs of text files in repositories
"""



import sys
import argparse
import os
import time
import shutil
import logging
import getpass
import json
import datetime
import base64

import git
import requests
import pandas as pd



# This domain is used in every GitHub REST API call
GH_API_URL = 'https://api.github.com'

# Prepare a requests Session object for connection-pooling and Keep-Alive
GH_SESSION = requests.Session()

# Directory where Git repo(s) are checked out
WORK_DIR = '/tmp'



def raise_on_err(resp):
    """Raise an Exception if given requests Response object indicates failure.
    """
    if isinstance(resp, requests.models.Response) and not resp.ok:
        if hasattr(resp, 'reason'):
            raise Exception('GitHub API responded with reason: "{}"'.format(resp.reason))
        elif hasattr(resp, 'status_code'):
            raise Exception('GitHub API responded with status "{}"'.format(resp.status_code))
        else:
            raise Exception('GitHub API request failed')

    if isinstance(resp, dict) and 'message' in resp:
        raise Exception('GitHub API returned the following error message: "{}"'.format(resp['message']))


def clone_repo(to_path, uri='ContinuumIO/anaconda'):
    """Clone repo into a fresh directory.
    Do it in such a way that the operation is "atomic", so that checking
    for directory existence is sufficient to tell whether the repository
    was fully cloned or not.
    """
    url = 'https://github.com/{}.git'.format(uri)

    if os.path.isdir(to_path):
        shutil.rmtree(to_path)
    if not os.path.isdir(os.path.dirname(to_path)):
        os.mkdir(os.path.dirname(to_path))
    if os.path.exists(to_path+'.tmp'):
        shutil.rmtree(to_path+'.tmp')

    class DisplayProgress(git.RemoteProgress):
        def update(self, op, cur_count, max_count=None, message=''):
            print('.', end='', flush=True)

    # Clone the repo
    progressDisplay = DisplayProgress()
    logging.info('Cloning {}'.format(url))
    git.Repo.clone_from(url, to_path+'.tmp', progress=progressDisplay)
    shutil.move(to_path+'.tmp', to_path)
    print()


def get_recipe_contents(repo_dir):
    contents_dict = {}

    # Collect recipe directories into a set.
    # Recipe directories are identified by their meta.yaml files
    recipe_dirs = set()
    for root, _, files in os.walk(repo_dir):
        # Special cases to skip, that clearly aren't recipes
        if '.git' in root or (os.path.basename(root) == 'BUILD' and
                              os.path.basename(os.path.dirname(root)) == 'anaconda-recipes'):
            continue

        for fname in files:
            if fname == 'meta.yaml':
                recipe_dirs.add(root)
                break

    # Get the contents of each recipe directory once the recipe name is found
    for recipe_dir in recipe_dirs:
        recipe_name = None
        tmp = recipe_dir
        # Handle the case where the recipe is AnacondaRecipes
        if os.path.basename(tmp) == 'recipe':
            tmp = os.path.dirname(tmp)
            recipe_name = os.path.basename(tmp).rsplit('-recipe', 1)[0]
        else:
            recipe_name = os.path.basename(tmp)
        assert recipe_name is not None

        # Get the recipe contents as bytes, in order by file
        recipe_contents = b''
        for root, _, files in os.walk(recipe_dir):
            if '.git' in root:
                continue

            for fname in sorted(files):
                fpath = os.path.join(root, fname)
                with open(fpath, 'rb') as fp:
                    file_contents = fp.read()
                recipe_contents += file_contents

        contents_dict[recipe_name] = base64.b64encode(recipe_contents)
    return contents_dict


def get_anaconda_repo_contents(use_cache=False):
    recipe_content_dict = {}

    logging.info('Fetching all AnacondaRecipes repositories...')

    # The GitHub API paginates on repository requests; here we find the number of pages
    pages_header = GH_SESSION.head(GH_API_URL+'/orgs/AnacondaRecipes/repos?page=1')
    raise_on_err(pages_header)
    npages = int(pages_header.links['last']['url'].rsplit('=', 1)[1])

    seen = set()
    for page_ct in range(1, npages+1):
        public_repos_resp = GH_SESSION.get(GH_API_URL+'/orgs/AnacondaRecipes/repos?page='+str(page_ct))
        public_repos = public_repos_resp.json()
        raise_on_err(public_repos)
        for public_repo in public_repos:
            repo_name = public_repo['name']
            assert repo_name.endswith('-recipe')
            recipe_name = repo_name.split('-recipe', 1)[0]
            if recipe_name in seen: # Odd case with GitHub API
                continue
            seen.add(recipe_name)

            repo_dir = os.path.join(WORK_DIR, 'AnacondaRecipes', repo_name)
            if not use_cache or not os.path.isdir(repo_dir):
                clone_repo(repo_dir, uri='AnacondaRecipes/'+repo_name)
            repo = git.Repo(repo_dir)
            repo.git.checkout('master')

            recipe_dir = os.path.join(repo_dir, 'recipe')
            recipe_content_dict.update(get_recipe_contents(recipe_dir))

    logging.info('Found {} repositories.'.format(len(recipe_content_dict)))

    return recipe_content_dict


def add_git_subtrees(repo, recipe_repos):
    """Add a git subtree for each AnacondaRecipe repository."""
    for repo_ct, (recipe_name, recipe_repo) in enumerate(recipe_repos.items()):
        repo_url = 'git://github.com/AnacondaRecipes/{}-recipe'.format(recipe_name)
        subtree_dest = os.path.join('prefix', recipe_name)

        # Add the git subtree as a distinct directory in the git repo
        logging.info('Adding subtree {}/{} at "{}" (-> {})'.format(repo_ct+1, len(recipe_repos), subtree_dest, repo_url))
        #repo.git.remote('add', recipe_name, repo_url)
        repo.git.subtree('add', repo_url, 'master', prefix=subtree_dest, annotate='[anaconda-recipes]')


def set_gh_auth():
    """Prompt for user's GitHub username and password. Update the global
    requests Session object with this information so that we don't have to pass
    around credentials.
    """
    global GH_SESSION
    gh_username = input('GitHub username: ')
    gh_password = getpass.getpass(prompt='GitHub password: ')
    GH_SESSION.auth = (gh_username, gh_password)


def get_anaconda_recipe_contents(use_cache=False):
    dist_packages = {}
    dist_public_packages = {}

    logging.info('Fetching all Anaconda distribution recipes...')
    anaconda_repo_dir = os.path.join(WORK_DIR, 'anaconda')
    if not use_cache or not os.path.isdir(anaconda_repo_dir):
        clone_repo(anaconda_repo_dir, uri='ContinuumIO/anaconda')
    repo = git.Repo(anaconda_repo_dir)
    repo.git.checkout('master')
    pkgs_dir = os.path.join(anaconda_repo_dir, 'packages')
    for recipe_dir in os.listdir(pkgs_dir):
        recipe_dpath = os.path.join(pkgs_dir, recipe_dir)
        dist_packages.update(get_recipe_contents(recipe_dpath))
    logging.info('Found {} total distribution recipes.'.format(len(dist_packages)))

    logging.info('Fetching public Anaconda distribution recipes...')
    anaconda_recipes_repo_dir = os.path.join(WORK_DIR, 'anaconda-recipes')
    if not use_cache or not os.path.isdir(anaconda_recipes_repo_dir):
        clone_repo(anaconda_recipes_repo_dir, uri='ContinuumIO/anaconda-recipes')
    repo = git.Repo(anaconda_recipes_repo_dir)
    repo.git.checkout('master')
    for recipe_dir in os.listdir(anaconda_recipes_repo_dir):
        recipe_dpath = os.path.join(anaconda_recipes_repo_dir, recipe_dir)
        dist_public_packages.update(get_recipe_contents(recipe_dpath))
    logging.info('Found {} public distribution recipes.'.format(len(dist_public_packages)))

    return dist_packages, dist_public_packages


def calc_recipe_diff(public_recipes, dist_public_recipes):
    """Return a Pandas dataframe object representing the diff between public_recipes and dist_public_recipes.
    """
    logging.info('Calculating recipe differences...')

    public_repo_names = set(public_recipes)
    dist_repo_names = set(dist_public_recipes)
    new_external_recipes = public_repo_names - dist_repo_names
    new_internal_recipes = dist_repo_names - public_repo_names
    common_recipes = dist_repo_names.intersection(public_repo_names)

    recipes = sorted(dist_repo_names.union(public_repo_names))
    df = pd.DataFrame(columns=['Recipe', 'AnacondaRecipes', 'ContinuumIO', 'ContentsEqual'])
    df['Recipe'] = recipes
    df['AnacondaRecipes'] = df['Recipe'].map(lambda r: r in public_recipes)
    df['ContinuumIO'] = df['Recipe'].map(lambda r: r in dist_public_recipes)
    df['ContentsEqual'] = df['Recipe'].map(lambda r: public_recipes[r] == dist_public_recipes[r] if r in public_recipes and r in dist_public_recipes else pd.np.nan)

    return df


def get_recipe_diff(debug=False):
    """Return a Pandas dataframe representing a "diff" between AnacondaRecipes
    and ContinuumIO organizations' views of public conda packages.
    """
    response_types = 'public_recipes', 'dist_recipes', 'dist_public_recipes'
    external_recipes = get_anaconda_repo_contents(use_cache=debug)
    dist_recipes, dist_public_recipes = get_anaconda_recipe_contents(use_cache=debug)

    logging.info('AnacondaRecipes repos: {}'.format(len(external_recipes)))
    logging.info('anaconda-recipes (mirrored) recipes: {}'.format(len(dist_public_recipes)))
    logging.info('Anaconda Distribution (open source + internal) recipes: {}'.format(len(dist_recipes)))

    diff_df = calc_recipe_diff(external_recipes, dist_public_recipes)
    return diff_df


def complete_action(action, debug=False):
    """Return an integer status code - 0 meaning success, nonzero meaning
    failure - after executing user-requested action.

    action can be one of the following: ('diff', 'externalize', 'internalize')
    debug=True enables the GitHub API response cache.
    """
    logging.info('Executing action "{}".'.format(action))
    if debug:
        logging.info('Debug mode enabled.')

    set_gh_auth() # Fixes "API rate limit exceeded" issue

    diff_df = get_recipe_diff(debug=debug)
    logging.info('\n'+str(diff_df))

    if action == 'diff':
        return 0

    if action == 'externalize':
        recipes_to_update = diff_df[~diff_df.fillna(True)['ContentsEqual']]
        for (recipe_name,) in recipes_to_update[['Recipe']].itertuples(index=False):
            external_recipe_dpath = os.path.join(WORK_DIR, 'AnacondaRecipes', recipe_name+'-recipe', 'recipe')
            internal_recipe_dpath = os.path.join(WORK_DIR, 'anaconda-recipes', recipe_name)

            # Checkout a new branch, commit internal stuff there, and merge master into it
            external_repo = git.Repo(os.path.join(WORK_DIR, 'AnacondaRecipes', recipe_name+'-recipe'))

            today = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
            branch_name = 'externalize_{}'.format(today)
            external_repo.git.checkout('master', b=branch_name)
            shutil.rmtree(external_recipe_dpath) # Remove files that may no longer be needed
            shutil.copytree(internal_recipe_dpath, external_recipe_dpath) # Copy internal files over
            for untracked_file in external_repo.untracked_files: # Add untracked (new) files
                external_repo.git.add(untracked_file)
            external_repo.git.commit('-a', '-m', 'Update with internal changes')
            external_repo.git.merge('master')

            if debug:
                logging.info('Debug mode: Not opening PR for recipe "{}" with branch "{}"...'.format(recipe_name, branch_name))
            else:
                logging.info('Opening PR for recipe "{}" with branch "{}"...'.format(recipe_name, branch_name))
                external_repo.git.push('origin', branch_name)
                GH_SESSION.post(GH_API_URL+'/repos/AnacondaRecipes/'+recipe_name+'-recipe/pulls',
                                data=dict(title='Update with internal changes',
                                          body='',
                                          head=branch_name,
                                          base='master'))
    elif action == 'internalize':
        internal_repo = git.Repo(os.path.join(WORK_DIR, 'anaconda'))
        today = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
        branch_name = 'internalize_{}'.format(today)
        internal_repo.git.checkout('master', b=branch_name)

        recipes_to_update = diff_df[~diff_df.fillna(True)['ContentsEqual']]
        for (recipe_name,) in recipes_to_update[['Recipe']].itertuples(index=False):
            external_recipe_dpath = os.path.join(WORK_DIR, 'AnacondaRecipes', recipe_name+'-recipe', 'recipe')
            internal_recipe_dpath = os.path.join(WORK_DIR, 'anaconda', 'packages', recipe_name)

            # Some anaconda-recipes packages are not inside anaconda!
            if not os.path.isdir(internal_recipe_dpath):
                logging.error('Package "{}" exists in anaconda-recipes, but not inside anaconda. Ignoring...'.format(recipe_name))
                continue

            shutil.rmtree(internal_recipe_dpath) # Remove files that may no longer be needed
            shutil.copytree(external_recipe_dpath, internal_recipe_dpath) # Copy internal files over
            for untracked_file in internal_repo.untracked_files: # Add untracked (new) files
                internal_repo.git.add(untracked_file)
            internal_repo.git.commit('-a', '-m', 'Update {} recipe with external changes'.format(recipe_name))
            internal_repo.git.merge('master')

        if debug:
            logging.info('Debug mode: Not opening PR for anaconda with branch "{}"...'.format(branch_name))
        else:
            logging.info('Opening PR for anaconda with branch "{}"...'.format(branch_name))
            internal_repo.git.push('origin', branch_name)
            GH_SESSION.post(GH_API_URL+'/repos/ContinuumIO/anaconda/pulls',
                            data=dict(title='Update with AnacondaRecipes changes',
                                      body='',
                                      head=branch_name,
                                      base='master'))
    else:
        raise Exception('If the code reaches this point, the argument parsing logic needs to be corrected.')

    return 0



def main(argv):
    """Parse command-line args, handle requested user actions. Return an integer
    status code.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('diff', 'externalize', 'internalize'), help='Action')
    parser.add_argument('-l', '--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default='INFO', help='Logging level')
    parser.add_argument('-d', '--debug', action='store_true', help='Speed up debugging by caching GitHub API responses on disk, and reading these responses on future runs in --debug mode. This also avoids user/pass prompt after cache is created.')
    args = parser.parse_args(argv[1:])

    logging.basicConfig(format='[%(asctime)s] %(levelname)s [%(module)s]: %(message)s', level=getattr(logging, args.log_level))

    status_code = complete_action(args.action, debug=args.debug)

    return status_code



if __name__ == '__main__':
    sys.exit(main(sys.argv))
