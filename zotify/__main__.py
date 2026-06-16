#! /usr/bin/env python3

"""
Zotify
It's like youtube-dl, but for that other music platform.
"""

import argparse


class DepreciatedAction(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        if "help" in kwargs:
            kwargs["help"] = "[DEPRECATED] " + kwargs["help"]
        super().__init__(option_strings, dest, **kwargs)
    
    def __call__(self, parser, namespace, values, option_string=None):
        from zotify.termoutput import Printer
        Printer.depreciated_warning(option_string, self.help, CONFIG=False)
        setattr(namespace, self.dest, values)


DEPRECIATED_FLAGS = (
    {"flags":    ('-d', '--download',),     "type":    str,     "help":    'Use `--file` (`-f`) instead'},
)


def _load_config_arg_metadata():
    try:
        from zotify.config import CONFIG_VALUES, DEPRECIATED_CONFIGS
        return CONFIG_VALUES, DEPRECIATED_CONFIGS
    except ModuleNotFoundError:
        return {}, {}


def _version_string() -> str:
    try:
        from zotify.config import Zotify
        return f'Zotify {Zotify.VERSION}'
    except ModuleNotFoundError:
        return 'Zotify'


def build_parser():
    parser = argparse.ArgumentParser(prog='zotify',
        description='A music and podcast downloader needing only Python and FFMPEG.')
    
    parser.register('action', 'depreciated_ignore_warn', DepreciatedAction)
    
    # no args
    parser.add_argument('--version',
                        action='version',
                        version=_version_string(),
                        help='Show the version of Zotify')
    parser.add_argument('--persist',
                        action='store_true',
                        dest='persist',
                        help='Perform multiple queries with a single persistent Session')
    parser.add_argument('--update-config',
                        action='store_true',
                        dest='update_config',
                        help='Updates the `config.json` file while keeping all current settings unchanged')
    parser.add_argument('--update-archive',
                        action='store_true',
                        dest='update_archive',
                        help='Updates the `.song_archive` file entries with full paths while keeping non-findable entries unchanged')
    parser.add_argument('--debug',
                        action='store_true',
                        dest='debug',
                        help='Enable debug mode, prints extra information and creates a `config_DEBUG.json` file')
    parser.add_argument('-ns', '--no-splash',
                        action='store_true',
                        dest='no_splash',
                        help='Suppress the splash screen when loading')
    parser.add_argument('--web',
                        action='store_true',
                        dest='web',
                        help='Start the local Zotify web control panel')
    parser.add_argument('--web-host',
                        type=str,
                        default='127.0.0.1',
                        dest='web_host',
                        help='Host address for the local web control panel')
    parser.add_argument('--web-port',
                        type=int,
                        default=4382,
                        dest='web_port',
                        help='Port for the local web control panel')
    
    # with args
    parser.add_argument('-c', '--config', '--config-location',
                        type=str,
                        dest='config_location',
                        help='Specify a directory containing a Zotify `config.json` file to load settings')
    parser.add_argument('-u', '--username',
                        type=str,
                        dest='username',
                        help='Account username')
    parser.add_argument('--token',
                        type=str,
                        dest='token',
                        help='Authentication token')
    
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('urls',
                       type=str,
                       # action='extend',
                       nargs='*',
                       default="",
                       help='Download track(s), album(s), playlist(s), podcast episode(s), or artist(s) specified by the URL(s) passed as a command line argument(s). If an artist\'s URL is given, all albums by the specified artist will be downloaded. Can take multiple URLs as multiple arguments.')
    group.add_argument('-f', '--file',
                       type=str,
                       dest='file_of_urls',
                       help='Download all tracks/albums/episodes/playlists URLs within the file passed as argument')
    group.add_argument('-l', '--liked', '--liked-songs', 
                       action='store_true',
                       dest='liked_songs',
                       help='Download all Liked Songs on your account')
    group.add_argument('-p', '--playlist', '--playlists', '--user-playlists',
                       action='store_true',
                       dest='user_playlists',
                       help='Download playlist(s) created/saved by your account (interactive)')
    group.add_argument('-a', '--artist', '--artists', '--followed-artists',
                       action='store_true',
                       dest='followed_artists',
                       help='Download all songs by followed artist(s) (interactive)')
    group.add_argument('-m', '--album', '--albums', '--followed-albums',
                       action='store_true',
                       dest='followed_albums',
                       help='Download followed albums (interactive)')
    group.add_argument('-s', '--search',
                       type=str,
                       dest='search',
                       nargs='?',
                       const=' ',
                       help='Search tracks/albums/artists/playlists based on argument (interactive)')
    group.add_argument('-v', '--verify-library',
                       action='store_true',
                       dest='verify_library',
                       help='Check metadata for all tracks in ROOT_PATH or listed in SONG_ARCHIVE, updating the metadata if necessary. This will not download any new tracks, but may take a very, very long time.')
    modes = group._group_actions.copy()
    
    for flag in DEPRECIATED_FLAGS: 
        group.add_argument(*flag["flags"],
                           type=flag["type"],
                           help=flag["help"],
                           action='depreciated_ignore_warn')

    CONFIG_VALUES, DEPRECIATED_CONFIGS = _load_config_arg_metadata()
    
    for key in DEPRECIATED_CONFIGS:
        parser.add_argument(*DEPRECIATED_CONFIGS[key]['arg'],
                            type=str,
                            action='depreciated_ignore_warn',
                            help=f'Delete the `{key}` flag from the commandline call'
                            )
    
    for key in CONFIG_VALUES:
        parser.add_argument(*CONFIG_VALUES[key]['arg'],
                            type=str, #type conversion occurs in config.parse_arg_value()
                            dest=key.lower(),
                            default=None,
                            )
    parser.zotify_modes = modes
    return parser


def main():
    parser = build_parser()
    
    args = parser.parse_args()
    if args.web:
        from zotify.web import run_web
        run_web(args)
        return

    from zotify.app import client
    from zotify.config import Zotify

    Zotify.boot(args)
    client(args, parser.zotify_modes)


if __name__ == '__main__':
    main()
