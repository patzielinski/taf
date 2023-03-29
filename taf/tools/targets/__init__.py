import click
import taf.developer_tool as developer_tool
from taf.api.targets import (
    list_targets,
    add_target_repo,
    remove_target_repo,
    generate_repositories_json as geenerate_targets_repositroies_json
)
from taf.constants import DEFAULT_RSA_SIGNATURE_SCHEME
from taf.exceptions import TAFError


def attach_to_group(group):

    @group.group()
    def targets():
        pass


    @targets.command(context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    ))
    @click.argument("auth_path")
    @click.option("--target-name", default=None, help="Namespace prefixed name of the target repository")
    @click.option("--target-path", default=None, help="Target repository's filesystem path")
    @click.option("--role", default="targets", help="Signing role of the corresponding target file. "
                  "Can be a new role, in which case it will be necessary to enter its information when prompted")
    @click.option("--keystore", default=None, help="Location of the keystore files")
    @click.pass_context
    def add_repo(ctx, auth_path, target_path, target_name, role, keystore):
        """Add a new repository by adding it to repositories.json, creating a delegation (if targets is not
        its signing role) and adding and signing initial target files if the repository is found on the filesystem.
        All additional information that should be saved as the repository's custom content in `repositories.json`
        is specified by providing additional options. If the signing role does not exist, it will be created.
        E.g.

        `taf targets add-repo auth-path --target-name namespace1/repo` --serve latest --role role1`

        In this case, serve: latest will be added to the custom part of the target repository's entry in
        repositories.json.


        If the repository does ot exists, it is sufficient to provide its namespace prefixed name
        instead of the full filesystem path. If the repository's path is not provided, it is expected
        to be located in the same library root directory as the authentication repository,
        in a directory whose name corresponds to its name. If authentication repository's path
        is `E:\\examples\\root\\namespace\\auth`, and the target's namespace prefixed name is
        `namespace1\\repo1`, the target's path will be set to `E:\\examples\\root\\namespace1\\repo1`.
        """
        custom = {ctx.args[i][2:]: ctx.args[i+1] for i in range(0, len(ctx.args), 2)} if len(ctx.args) else {}
        add_target_repo(
            auth_path=auth_path,
            target_path=target_path,
            target_name=target_name,
            library_dir=None,
            role=role,
            keystore=keystore,
            custom=custom
        )



    @targets.command()
    @click.argument("path")
    @click.option("--library-dir", default=None, help="Directory where target repositories and, "
                  "optionally, authentication repository are located. If omitted it is "
                  "calculated based on authentication repository's path. "
                  "Authentication repo is presumed to be at library-dir/namespace/auth-repo-name")
    @click.option("--namespace", default=None, help="Namespace of the target repositories. "
                  "If omitted, it will be assumed that namespace matches the name of the "
                  "directory which contains the authentication repository")
    @click.option("--targets-rel-dir", default=None, help="Directory relative to which "
                  "urls of the target repositories are calculated. Only useful when "
                  "the target repositories do not have remotes set")
    @click.option("--custom", default=None, help="A dictionary containing custom "
                  "targets info which will be added to repositories.json")
    @click.option("--use-mirrors", is_flag=True, help="Whether to generate mirrors.json or not")
    def generate_repositories_json(path, library_dir, namespace, targets_rel_dir, custom, use_mirrors):
        """
        Generate repositories.json. This file needs to be one of the authentication repository's
        target files or the updater won't be able to validate target repositories.
        repositories.json is generated by traversing through all targets and adding an entry
        with the namespace prefixed name of the target repository as its key and the
        repository's url and custom data as its value.

        Target repositories are expected to be inside a directory whose name is equal to the specified
        namespace and which is located inside the root directory. If root directory is E:\\examples\\root
        and namespace is namespace1, target repositories should be in E:\\examples\\root\\namespace1.
        If the authentication repository and the target repositories are in the same root directory and
        the authentication repository is also directly inside a namespace directory, then the common root
        directory is calculated as two repositories up from the authentication repository's directory.
        Authentication repository's namespace can, but does not have to be equal to the namespace of target,
        repositories. If the authentication repository's path is E:\\root\\namespace\\auth-repo, root
        directory will be determined as E:\\root. If this default value is not correct, it can be redefined
        through the --library-dir option. If the --namespace option's value is not provided, it is assumed
        that the namespace of target repositories is equal to the authentication repository's namespace,
        determined based on the repository's path. E.g. Namespace of E:\\root\\namespace2\\auth-repo
        is namespace2.

        The url of a repository corresponds to its git remote url if set and to its location on the file
        system otherwise. Test repositories might not have remotes. If targets-rel-dir is specified
        and a repository does not have remote url, its url is calculated as a relative path to the
        repository's location from this path. There are two options of defining urls:
        1. Directly specifying all urls of all repositories directly inside repositories.json
        2. Using a separate mirrors.json file. This file will be generated only if use-mirrors flag is provided.
        The mirrors file consists of a list of templates which contain namespace (org name) and repo name as arguments.
        E.g. "https://github.com/{org_name}/{repo_name}.git". If a target repository's namespaced name
        is namespace1/target_repo1, its url will be calculated by replacing {org_name} with namespace1
        and {repo_name} with target_repo1 and would result in "https://github.com/namespace1/target_repo1.git"

        While urls are the only information that the updater needs, it is possible to add
        any other data using the custom option. Custom data can either be specified in a .json file
        whose path is provided when calling this command, or directly entered. Keys is this
        dictionary are names of the repositories whose custom data should be set and values are
        custom data dictionaries. For example:

        \b
        {
            "test/html-repo": {
                "type": "html"
            },
            "test/xml-repo": {
                "type": "xml"
            }
        }

        Note: this command does not automatically register repositories.json as a target file.
        It is recommended that the content of the file is reviewed before doing so manually.
        """
        geenerate_targets_repositroies_json(
            path, library_dir, namespace, targets_rel_dir, custom, use_mirrors
        )



    @targets.command()
    @click.argument("repo_path")
    @click.option("--commit", default=None, help="Starting authentication repository commit")
    @click.option("--output", default=None, help="File to which the resulting json will be written. "
                  "If not provided, the output will be printed to console")
    @click.option("--repo", multiple=True, help="Target repository whose historical data "
                  "should be collected")
    def export_history(repo_path, commit, output, repo):
        """Export lists of sorted commits, grouped by branches and target repositories, based
        on target files stored in the authentication repository. If commit is specified,
        only return changes made at that revision and all subsequent revisions. If it is not,
        start from the initial authentication repository commit.
        Repositories which will be taken into consideration when collecting targets historical
        data can be defined using the repo option. If no repositories are passed in, historical
        data will include all target repositories.
        to a file whose location is specified using the output option, or print it to
        console.
        """
        developer_tool.export_targets_history(repo_path, commit, output, repo)



    @targets.command()
    @click.argument("path")
    @click.option("--library-dir", default=None, help="Directory where target repositories and, "
                  "optionally, authentication repository are located. If omitted it is "
                  "calculated based on authentication repository's path. "
                  "Authentication repo is presumed to be at library-dir/namespace/auth-repo-name")
    def list(path, library_dir):
        """
        List targets
        """
        list_targets(path, library_dir)


    @targets.command()
    @click.argument("auth_path")
    @click.option("--target-name")
    @click.option("--keystore", default=None, help="Location of the keystore files")
    def remove_repo(auth_path, target_name, keystore):
        """Export lists of sorted commits, grouped by branches and target repositories, based
        on target files stored in the authentication repository. If commit is specified,
        only return changes made at that revision and all subsequent revisions. If it is not,
        start from the initial authentication repository commit.
        Repositories which will be taken into consideration when collecting targets historical
        data can be defined using the repo option. If no repositories are passed in, historical
        data will include all target repositories.
        to a file whose location is specified using the output option, or print it to
        console.
        """
        remove_target_repo(auth_path, target_name, keystore)


    @targets.command()
    @click.argument("path")
    @click.option("--keystore", default=None, help="Location of the keystore files")
    @click.option("--keys-description", help="A dictionary containing information about the "
                  "keys or a path to a json file which stores the needed information")
    @click.option("--scheme", default=DEFAULT_RSA_SIGNATURE_SCHEME, help="A signature scheme "
                  "used for signing")
    def sign(path, keystore, keys_description, scheme):
        """
        Register and sign target files. This means that all targets metadata files corresponding
        to roles responsible for updated target files are updated. Once the targets
        files are updated, so are snapshot and timestamp. All files are then signed. If the
        keystore parameter is provided, keys stored in that directory will be used for
        signing. If a needed key is not in that directory, the file can either be signed
        by manually entering the key or by using a Yubikey.
        """
        try:
            developer_tool.register_target_files(path, keystore=keystore,
                                                 roles_key_infos=keys_description,
                                                 scheme=scheme)
        except TAFError as e:
            click.echo()
            click.echo(str(e))
            click.echo()

    @targets.command()
    @click.argument("path")
    @click.option("--library-dir", default=None, help="Directory where target repositories and, "
                  "optionally, authentication repository are located. If omitted it is "
                  "calculated based on authentication repository's path. "
                  "Authentication repo is presumed to be at library-dir/namespace/auth-repo-name")
    @click.option("--target-type", multiple=True, help="Types of target repositories whose corresponding "
                  "target files should be updated and signed. Should match a target type defined in "
                  "repositories.json")
    @click.option("--keystore", default=None, help="Location of the keystore files")
    @click.option("--keys-description", help="A dictionary containing information about the "
                  "keys or a path to a json file which stores the needed information")
    @click.option("--scheme", default=DEFAULT_RSA_SIGNATURE_SCHEME, help="A signature scheme "
                  "used for signing")
    def update_and_sign_targets(path, library_dir, target_type, keystore, keys_description, scheme):
        """
        Update target files corresponding to target repositories specified through the target type parameter
        by writing the current top commit and branch name to the target files. Sign the updated files
        and then commit.
        """

        if not len(target_type):
            click.echo("Specify at least one target type")
            return
        try:
            developer_tool.update_and_sign_targets(
                path,
                library_dir,
                target_type,
                keystore=keystore,
                roles_key_infos=keys_description,
                scheme=scheme)
        except TAFError as e:
            click.echo()
            click.echo(str(e))
            click.echo()

    @targets.command()
    @click.argument("path")
    @click.option("--library-dir", default=None, help="Directory where target repositories and, "
                  "optionally, authentication repository are located. If omitted it is "
                  "calculated based on authentication repository's path. "
                  "Authentication repo is presumed to be at library-dir/namespace/auth-repo-name")
    @click.option("--namespace", default=None, help="Namespace of the target repositories. "
                  "If omitted, it will be assumed that namespace matches the name of the "
                  "directory which contains the authentication repository")
    @click.option("--add-branch", default=False, is_flag=True, help="Whether to add name of "
                  "the current branch to target files")
    def update_repos_from_fs(path, library_dir, namespace, add_branch):
        """
        Update target files corresponding to target repositories by traversing through the root
        directory. Does not automatically sign the metadata files.
        Note: if repositories.json exists, it is better to call update_repos_from_repositories_json

        Target repositories are expected to be inside a directory whose name is equal to the specified
        namespace and which is located inside the root directory. If root directory is E:\\examples\\root
        and namespace is namespace1, target repositories should be in E:\\examples\\root\\namespace1.
        If the authentication repository and the target repositories are in the same root directory and
        the authentication repository is also directly inside a namespace directory, then the common root
        directory is calculated as two repositories up from the authentication repository's directory.
        Authentication repository's namespace can, but does not have to be equal to the namespace of target,
        repositories. If the authentication repository's path is E:\\root\\namespace\\auth-repo, root
        directory will be determined as E:\\root. If this default value is not correct, it can be redefined
        through the --library-dir option. If the --namespace option's value is not provided, it is assumed
        that the namespace of target repositories is equal to the authentication repository's namespace,
        determined based on the repository's path. E.g. Namespace of E:\\root\\namespace2\\auth-repo
        is namespace2.

        Once the directory containing all target directories is determined, it is traversed through all
        git repositories in that directory, apart from the authentication repository if it is found.
        For each found repository the current top commit and branch (if called with the
        --add-branch flag) are written to the corresponding target files. Target files are files
        inside the authentication repository's target directory. For example, for a target repository
        namespace1/target1, a file called target1 is created inside the targets/namespace1 authentication
        repository's direcotry.
        """
        developer_tool.update_target_repos_from_fs(path, library_dir, namespace, add_branch)

    @targets.command()
    @click.argument("path")
    @click.option("--library-dir", default=None, help="Directory where target repositories and, "
                  "optionally, authentication repository are located. If omitted it is "
                  "calculated based on authentication repository's path. "
                  "Authentication repo is presumed to be at library-dir/namespace/auth-repo-name")
    @click.option("--namespace", default=None, help="Namespace of the target repositories. "
                  "If omitted, it will be assumed that namespace matches the name of the "
                  "directory which contains the authentication repository")
    @click.option("--add-branch", default=False, is_flag=True, help="Whether to add name of "
                  "the current branch to target files")
    def update_repos_from_repositories_json(path, library_dir, namespace, add_branch):
        """
        Update target files corresponding to target repositories by traversing through repositories
        specified in repositories.json which are located inside the specified targets directory without
        signing the metadata files.

        Target repositories are expected to be inside a directory whose name is equal to the specified
        namespace and which is located inside the root directory. If root directory is E:\\examples\\root
        and namespace is namespace1, target repositories should be in E:\\examples\\root\\namespace1.
        If the authentication repository and the target repositories are in the same root directory and
        the authentication repository is also directly inside a namespace directory, then the common root
        directory is calculated as two repositories up from the authentication repository's directory.
        Authentication repository's namespace can, but does not have to be equal to the namespace of target,
        repositories. If the authentication repository's path is E:\\root\\namespace\\auth-repo, root
        directory will be determined as E:\\root. If this default value is not correct, it can be redefined
        through the --library-dir option. If the --namespace option's value is not provided, it is assumed
        that the namespace of target repositories is equal to the authentication repository's namespace,
        determined based on the repository's path. E.g. Namespace of E:\\root\\namespace2\\auth-repo
        is namespace2.

        Once the directory containing all target directories is determined, it is traversed through all
        git repositories in that directory which are listed in repositories.json.
        This means that for each found repository the current top commit and branch (if called with the
        --add-branch flag) are written to the target corresponding target files. Target files are files
        inside the authentication repository's target directory. For example, for a target repository
        namespace1/target1, a file called target1 is created inside the targets/namespace1
        authentication repo direcotry.
        """
        developer_tool.update_target_repos_from_repositories_json(path, library_dir, namespace, add_branch)
