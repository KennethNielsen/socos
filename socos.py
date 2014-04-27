#!/usr/bin/env python

""" socos is a commandline tool for controlling Sonos speakers """

from __future__ import print_function


# Will be parsed by setup.py to determine package metadata
__author__ = 'SoCo team <python-soco @googlegroups.com>'
__version__ = '0.1'
__website__ = 'https://github.com/SoCo/socos'
__license__ = 'MIT License'


import sys
from os import path
from collections import OrderedDict
import sqlite3
import json
import shlex

try:
    # pylint: disable=import-error
    import colorama
except ImportError:
    # pylint: disable=invalid-name
    colorama = None

try:
    import readline
except ImportError:
    # pylint: disable=invalid-name
    readline = None

try:
    # pylint: disable=redefined-builtin,invalid-name,undefined-variable
    input = raw_input
except NameError:
    # raw_input has been renamed to input in Python 3
    pass

import soco
from soco.data_structures import MLTrack, MLAlbum, MLArtist, MLPlaylist


# pylint: disable=too-many-instance-attributes
class MusicLibrary(object):
    """Class that implements the music library support for socos"""

    def __init__(self):
        self.connection = None
        self.cursor = None
        self.create_statements = [
            'CREATE TABLE tracks (title text, album text, artist text, '
            'content text)',
            'CREATE TABLE albums (title text, artist text, content text)',
            'CREATE TABLE artists (title text, content text)',
            'CREATE TABLE playlists (title text, content text)',
        ]
        self.drop_statements = [
            'DROP TABLE tracks', 'DROP TABLE albums', 'DROP TABLE artists',
            'DROP TABLE playlists'
        ]
        self.data_types = ['playlists', 'artists', 'albums', 'tracks']
        self.ml_classes = {'tracks': MLTrack, 'albums': MLAlbum,
                           'artists': MLArtist, 'playlists': MLPlaylist}
        self.cached_searches = OrderedDict()
        self.cache_length = 10
        self.print_patters = {
            u'tracks': '\'{title}\' on \'{album}\' by \'{creator}\'',
            u'albums': '\'{title}\' by \'{creator}\'',
            u'artists': '\'{title}\'',
            u'playlists': '\'{title}\''
        }

    def _open_db(self):
        """Open a connection to the db"""
        if not self.connection:
            self.connection = sqlite3.connect(self._get_path())
            self.cursor = self.connection.cursor()

    @staticmethod
    def _get_path():
        """Return the platform dependent path for the data base"""
        out = path.join(path.expanduser('~'), '.config', 'socos',
                        'musiclib.db')
        return out

    def index(self, sonos):
        """Update the local index"""
        self._open_db()
        # Drop old tables
        query = 'SELECT name FROM sqlite_master WHERE type = "table"'
        self.cursor.execute(query)
        number_of_tables = len(self.cursor.fetchall())
        if number_of_tables == 4:
            yield 'Deleting tables'
            for drop in self.drop_statements:
                self.cursor.execute(drop)
        self.connection.commit()

        # Form new
        yield 'Creating tables'
        for create in self.create_statements:
            self.cursor.execute(create)
        self.connection.commit()

        for data_type in self.data_types:
            for string in self._index_single_type(sonos, data_type):
                yield string

    def _index_single_type(self, sonos, data_type):
        """Index a single type if data"""
        fields = self._get_columns(data_type)
        # Artist is called creator in the UPnP data structures
        if 'artist' in fields:
            fields[fields.index('artist')] = 'creator'

        # E.g: INSERT INTO tracks VALUES (?,?,?,?)
        query = 'INSERT INTO {} VALUES ({})'.format(
            data_type, ','.join(['?'] * len(fields)))

        # For readability
        get_ml_inf = sonos.get_music_library_information

        total = get_ml_inf(data_type, 0, 1)['total_matches']

        yield 'Adding: {}'.format(data_type)
        count = 0
        while count < total:
            search = get_ml_inf(data_type, start=count, max_items=1000)
            for item in search['item_list']:
                values = [getattr(item, field) for field in
                          fields[:-1]]
                values.append(json.dumps(item.to_dict))
                self.cursor.execute(query, values)
            self.connection.commit()

            # Status
            count += search['number_returned']
            yield '{{: >3}}%  {{: >{0}}} out of {{: >{0}}}'\
                .format(len(str(total)))\
                .format(count * 100 / total, count, total)

    def _get_columns(self, table):
        """Return the names of the columns in the table"""
        query = 'PRAGMA table_info({})'.format(table)
        self.cursor.execute(query)
        # The table descriptions look like: (0, u'title', u'text', 0, None, 0)
        return [element[1] for element in self.cursor.fetchall()]

    def tracks(self, sonos, *args):
        """Search for and possibly play tracks"""
        for string in self._search_and_play(sonos, 'tracks', *args):
            yield string

    def albums(self, sonos, *args):
        """Search for and possibly play albums"""
        for string in self._search_and_play(sonos, 'albums', *args):
            yield string

    def artists(self, sonos, *args):
        """Search for and possibly play all by artists"""
        for string in self._search_and_play(sonos, 'artists', *args):
            yield string

    def playlists(self, sonos, *args):
        """Search for and possibly play imported playlists"""
        for string in self._search_and_play(sonos, 'playlists', *args):
            yield string

    def _search_and_play(self, sonos, data_type, *args):
        """Perform a music library search and possibly play and item"""
        self._open_db()
        if len(args) < 1:
            message = 'Search term missing. Can be on the form \'field=text\' '\
              'or \'text\' e.g. \'artist=Metallica\'. If no field is given '\
              '\'title\' field is used.'
            raise TypeError(message)
        import time
        t0 = time.time()
        results = self._search(sonos, data_type, *args)
        print(time.time() - t0)

        if len(args) == 1:
            for string in self._print_results(data_type, results):
                yield string
        elif len(args) == 3:
            yield self._play(sonos, data_type, results, *args)
        else:
            message = 'Incorrect play syntax, must be \'search action '\
              'number\' e.g. \'artist=Metallica add 7\'. Action can be '\
              '\'add\' or \'replace'
            raise TypeError(message)

    def _search(self, sonos, data_type, *args):
        """Perform the search"""
        # Process search term
        search_string = args[0]
        if search_string.count('=') == 0:
            field = 'title'
            search = search_string
        elif search_string.count('=') == 1:
            field, search = search_string.split('=')
        else:
            message = '= signs are not allowed in the search string'
            raise TypeError(message)

        # Do the search, if it has not been cached
        search = search.join(['%', '%'])
        if (data_type, field, search) in self.cached_searches:
            print('cached')
            results = self.cached_searches[(data_type, field, search)]
        else:
            print('searched')
            if field in self._get_columns(data_type)[:-1]:
                query = 'SELECT * FROM {} WHERE {} LIKE ?'.format(data_type,
                                                                  field)
                self.cursor.execute(query, [search])
                results = self.cursor.fetchall()
                self.cached_searches[(data_type, field, search)] = results
                while len(self.cached_searches) > self.cache_length:
                    self.cached_searches.popitem(last=False)
            else:
                message = 'The search field \'{}\' is unknown. Only {} is '\
                    'allowed'.format(field, self._get_columns(data_type)[:-1])
                raise TypeError(message)
        return results

    def _play(self, sonos, data_type, results, *args):
        """Play music library item from search"""
        action, number = args[1:]
        # Check action
        if action not in ['add', 'replace']:
            message = 'Action must be \'add\' or \'replace\''
            raise TypeError(message)

        # Convert and check number
        try:
            number = int(number) - 1
        except ValueError:
            raise TypeError('Play number must be parseable as integer')
        if number not in range(len(results)):
            if len(results) == 0:
                message = 'No results to play from'
            elif len(results) == 1:
                message = 'Play number can only be 1'
            else:
                message = 'Play number has to be in the range from 1 to {}'.\
                  format(len(results))
            raise TypeError(message)

        # The last item in the search is the content dict in json
        item_dict = json.loads(results[number][-1])
        item = self.ml_classes[data_type].from_dict(item_dict)
        player_state = state(sonos)
        if action == 'replace':
            sonos.clear_queue()
        sonos.add_to_queue(item)
        out = 'Added to queue: \'{}\''
        if action == 'replace' and player_state == 'PLAYING':
            sonos.play()
            out = 'Queue replaced with: \'{}\''
        title = item.title
        if hasattr(title, 'decode'):
            title = title.encode('utf-8')
        return out.format(title)

    def _print_results(self, data_type, results):
        """Print the results"""
        index_length = len(str(len(results)))
        for index, item in enumerate(results):
            item_dict = json.loads(item[-1])
            for key, value in item_dict.items():
                if hasattr(value, 'decode'):
                    item_dict[key] = value.encode('utf-8')
            number = '({{: >{}}}) '.format(index_length).format(index + 1)
            # pylint: disable=star-args
            yield number + self.print_patters[data_type].format(**item_dict)


# current speaker (used only in interactive mode)
CUR_SPEAKER = None
# Instance of music library class
MUSIC_LIB = MusicLibrary()


def main():
    """ main switches between (non-)interactive mode """
    args = sys.argv[1:]

    if args:
        # process command and exit
        process_cmd(args)
    else:
        # start interactive shell
        shell()


def process_cmd(args):
    """ Processes a single command """

    cmd = args.pop(0).lower()

    if cmd not in COMMANDS:
        err('Unknown command "{cmd}"'.format(cmd=cmd))
        err(get_help())
        return False

    func, args = _check_args(cmd, args)

    try:
        result = _call_func(func, args)
    except TypeError as ex:
        err(ex)
        return

    # colorama.init() takes over stdout/stderr to give cross-platform colors
    if colorama:
        colorama.init()

    # process output
    if result is None:
        pass

    elif hasattr(result, '__iter__'):
        try:
            for line in result:
                print(line)
        except TypeError as ex:
            err(ex)
            return

    else:
        print(result)

    # Release stdout/stderr from colorama
    if colorama:
        colorama.deinit()


def _call_func(func, args):
    """ handles str-based functions and calls appropriately """

    # determine how to call function
    if isinstance(func, str):
        sonos = args.pop(0)
        method = getattr(sonos, func)
        return method(*args)  # pylint: disable=star-args

    else:
        return func(*args)  # pylint: disable=star-args


def _check_args(cmd, args):
    """ checks if func is called for a speaker and updates 'args' """

    req_ip, func = COMMANDS[cmd]

    if not req_ip:
        return func, args

    if not CUR_SPEAKER:
        if not args:
            err('Please specify a speaker IP for "{cmd}".'.format(cmd=cmd))
            return None, None
        else:
            speaker_spec = args.pop(0)
            sonos = soco.SoCo(speaker_spec)
            args.insert(0, sonos)
    else:
        args.insert(0, CUR_SPEAKER)

    return func, args


def shell():
    """ Start an interactive shell """

    if readline is not None:
        readline.parse_and_bind('tab: complete')
        readline.set_completer(complete_command)
        readline.set_completer_delims(' ')

    while True:
        try:
            # Not sure why this is necessary, as there is a player_name attr
            # pylint: disable=no-member
            if CUR_SPEAKER:
                line = input('socos({speaker}|{state})> '.format(
                    speaker=CUR_SPEAKER.player_name,
                    state=state(CUR_SPEAKER).title()).encode('utf-8'))
            else:
                line = input('socos> ')
        except EOFError:
            print('')
            break
        except KeyboardInterrupt:
            print('')
            continue

        line = line.strip()
        if not line:
            continue

        try:
            args = shlex.split(line)
        except ValueError as value_error:
            err('Syntax error: %(error)s' % {'error': value_error})
            continue

        try:
            process_cmd(args)
        except KeyboardInterrupt:
            err('Keyboard interrupt.')
        except EOFError:
            err('EOF.')


def complete_command(text, context):
    """ auto-complete commands

    text is the text to be auto-completed
    context is an index, increased for every call for "text" to get next match
    """
    matches = [cmd for cmd in COMMANDS.keys() if cmd.startswith(text)]
    return matches[context]


def adjust_volume(sonos, operator):
    """ Adjust the volume up or down with a factor from 1 to 100 """
    factor = get_volume_adjustment_factor(operator)
    if not factor:
        return False

    vol = sonos.volume

    if operator[0] == '+':
        if (vol + factor) > 100:
            factor = 1
        sonos.volume = (vol + factor)
        return sonos.volume
    elif operator[0] == '-':
        if (vol - factor) < 0:
            factor = 1
        sonos.volume = (vol - factor)
        return sonos.volume
    else:
        err("Valid operators for volume are + and -")


def get_volume_adjustment_factor(operator):
    """ get the factor to adjust the volume with """
    factor = 1
    if len(operator) > 1:
        try:
            factor = int(operator[1:])
        except ValueError:
            err("Adjustment factor for volume has to be a int.")
            return
    return factor


def get_current_track_info(sonos):
    """ Show the current track """
    track = sonos.get_current_track_info()
    return (
        "Current track: %s - %s. From album %s. This is track number"
        " %s in the playlist. It is %s minutes long." % (
            track['artist'],
            track['title'],
            track['album'],
            track['playlist_position'],
            track['duration'],
        )
    )


def get_queue(sonos):
    """ Show the current queue """
    queue = sonos.get_queue()

    # pylint: disable=invalid-name
    ANSI_BOLD = '\033[1m'
    ANSI_RESET = '\033[0m'

    current = int(sonos.get_current_track_info()['playlist_position'])

    queue_length = len(queue)
    padding = len(str(queue_length))

    for idx, track in enumerate(queue, 1):
        if idx == current:
            color = ANSI_BOLD
        else:
            color = ANSI_RESET

        idx = str(idx).rjust(padding)
        yield (
            "%s%s: %s - %s. From album %s." % (
                color,
                idx,
                track['artist'],
                track['title'],
                track['album'],
            )
        )


def err(message):
    """ print an error message """
    print(message, file=sys.stderr)


def play_index(sonos, index):
    """ Play an item from the playlist """
    queue_length = len(sonos.get_queue())
    try:
        index = int(index) - 1
        if index >= 0 and index < queue_length:
            position = sonos.get_current_track_info()['playlist_position']
            current = int(position) - 1
            if index != current:
                return sonos.play_from_queue(index)
        else:
            raise ValueError()
    except ValueError():
        return "Index has to be a integer within \
                the range 1 - %d" % queue_length


def list_ips():
    """ List available devices """
    sonos = soco.SonosDiscovery()
    return sonos.get_speaker_ips()


def speaker_info(sonos):
    """ Information about a speaker """
    infos = sonos.get_speaker_info()
    return ('%s: %s' % (i, infos[i]) for i in infos)


def volume(sonos, *args):
    """ Change or show the volume of a device """
    if args:
        operator = args[0].lower()
        adjust_volume(sonos, operator)

    return sonos.volume


def exit_shell():
    """ Exit socos """
    sys.exit(0)


def play(sonos, *args):
    """ Start playing """
    if args:
        idx = args[0]
        play_index(sonos, idx)
    else:
        sonos.play()
    return get_current_track_info(sonos)


def play_next(sonos):
    """ Play the next track """
    sonos.next()
    return get_current_track_info(sonos)


def play_previous(sonos):
    """ Play the previous track """
    sonos.previous()
    return get_current_track_info(sonos)


def state(sonos):
    """ Get the current state of a device / group """
    return sonos.get_current_transport_info()['current_transport_state']


def set_speaker(ip_address):
    """ set the current speaker for the shell session """
    # pylint: disable=global-statement,fixme
    # TODO: this should be refactored into a class with instance-wide state
    global CUR_SPEAKER
    CUR_SPEAKER = soco.SoCo(ip_address)


def unset_speaker():
    """ resets the current speaker for the shell session """
    global CUR_SPEAKER  # pylint: disable=global-statement
    CUR_SPEAKER = None


def get_help(command=None):
    """ Prints a list of commands with short description """

    def _cmd_summary(item):
        """ Format command name and first line of docstring """
        name, func = item[0], item[1][1]
        if isinstance(func, str):
            func = getattr(soco.SoCo, func)
        doc = getattr(func, '__doc__') or ''
        doc = doc.split('\n')[0].lstrip()
        return ' * {cmd:12s} {doc}'.format(cmd=name, doc=doc)

    if command and command in COMMANDS:
        func = COMMANDS[command][1]
        doc = getattr(func, '__doc__') or ''
        doc = [line.lstrip() for line in doc.split('\n')]
        out = '\n'.join(doc)
    else:
        # pylint: disable=bad-builtin
        texts = ['Available commands:']
        texts += map(_cmd_summary, COMMANDS.items())
        out = '\n'.join(texts)
    return out


# COMMANDS indexes commands by their name. Each command is a 2-tuple of
# (requires_ip, function) where function is either a callable, or a
# method name to be called on a SoCo instance (depending on requires_ip)
# If requires_ip is False, function must be a callable.
COMMANDS = {
    #  cmd         req IP  func
    'list':       (False, list_ips),
    'partymode':  (True, 'partymode'),
    'info':       (True, speaker_info),
    'play':       (True, play),
    'pause':      (True, 'pause'),
    'stop':       (True, 'stop'),
    'next':       (True, play_next),
    'previous':   (True, play_previous),
    'current':    (True, get_current_track_info),
    'queue':      (True, get_queue),
    'volume':     (True, volume),
    'state':      (True, state),
    'ml_index':   (True, MUSIC_LIB.index),
    'ml_tracks':  (True, MUSIC_LIB.tracks),
    'ml_albums':  (True, MUSIC_LIB.albums),
    'ml_artists': (True, MUSIC_LIB.artists),
    'ml_playlists': (True, MUSIC_LIB.playlists),
    'exit':       (False, exit_shell),
    'set':        (False, set_speaker),
    'unset':      (False, unset_speaker),
    'help':       (False, get_help),
}


if __name__ == '__main__':
    main()
