# standard modules
import collections
import contextlib
import functools
import warnings
import termios
import codecs
import curses
import locale
import select
import struct
import fcntl
import time
import tty
import sys
import os

try:
    from io import UnsupportedOperation as IOUnsupportedOperation
except ImportError:
    class IOUnsupportedOperation(Exception):
        """A dummy exception to take the place of Python 3's
        ``io.UnsupportedOperation`` in Python 2.5"""

# local imports
import formatters
import sequences
import keyboard


class Terminal(object):
    """An abstraction around terminal capabilities

    Unlike curses, this doesn't require clearing the screen before doing
    anything, and it's friendlier to use. It keeps the endless calls to
    ``tigetstr()`` and ``tparm()`` out of your code, and it acts intelligently
    when somebody pipes your output to a non-terminal.

    Instance attributes:

      ``stream``
        The stream the terminal outputs to. It's convenient to pass the stream
        around with the terminal; it's almost always needed when the terminal
        is and saves sticking lots of extra args on client functions in
        practice.
    """
    def __init__(self, kind=None, stream=None, force_styling=False):
        """Initialize the terminal.

        If ``stream`` is not a tty, I will default to returning an empty
        Unicode string for all capability values, so things like piping your
        output to a file won't strew escape sequences all over the place. The
        ``ls`` command sets a precedent for this: it defaults to columnar
        output when being sent to a tty and one-item-per-line when not.

        :arg kind: A terminal string as taken by ``setupterm()``. Defaults to
            the value of the ``TERM`` environment variable.
        :arg stream: A file-like object representing the terminal. Defaults to
            the original value of stdout, like ``curses.initscr()`` does.
        :arg force_styling: Whether to force the emission of capabilities, even
            if we don't seem to be in a terminal. This comes in handy if users
            are trying to pipe your output through something like ``less -r``,
            which supports terminal codes just fine but doesn't appear itself
            to be a terminal. Just expose a command-line option, and set
            ``force_styling`` based on it. Terminal initialization sequences
            will be sent to ``stream`` if it has a file descriptor and to
            ``sys.__stdout__`` otherwise. (``setupterm()`` demands to send them
            somewhere, and stdout is probably where the output is ultimately
            headed. If not, stderr is probably bound to the same terminal.)

            If you want to force styling to not happen, pass
            ``force_styling=None``.

        """
        global _CUR_TERM
        if stream is None:
            stream = sys.__stdout__
            self.stream_kb = sys.__stdin__.fileno()
        else:
            self.stream_kb = None

        try:
            stream_fd = (stream.fileno() if hasattr(stream, 'fileno')
                         and callable(stream.fileno) else None)
        except IOUnsupportedOperation:
            stream_fd = None

        self._is_a_tty = stream_fd is not None and os.isatty(stream_fd)
        self._does_styling = ((self.is_a_tty or force_styling) and
                              force_styling is not None)

        # keyboard input only valid when stream is sys.stdout

        # The desciptor to direct terminal initialization sequences to.
        # sys.__stdout__ seems to always have a descriptor of 1, even if output
        # is redirected.
        self._init_descriptor = (stream_fd is None and sys.__stdout__.fileno()
                                 or stream_fd)
        self._kind = kind or os.environ.get('TERM', 'unknown')
        if self.does_styling:
            # Make things like tigetstr() work. Explicit args make setupterm()
            # work even when -s is passed to nosetests. Lean toward sending
            # init sequences to the stream if it has a file descriptor, and
            # send them to stdout as a fallback, since they have to go
            # somewhere.
            try:
                curses.setupterm(self._kind, self._init_descriptor)
            except curses.error:
                warnings.warn('Failed to setupterm(kind=%s)' % (self._kind,))
                self._kind = None
                self._does_styling = False
            else:
                if _CUR_TERM is None or self._kind == _CUR_TERM:
                    _CUR_TERM = self._kind
                else:
                    warnings.warn(
                        'A terminal of kind "%s" has been requested; due to an'
                        ' internal python curses bug,  terminal capabilities'
                        ' for a terminal of kind "%s" will continue to be'
                        ' returned for the remainder of this process. see:'
                        ' https://github.com/erikrose/blessings/issues/33' % (
                            self._kind, _CUR_TERM,))

        if self.does_styling:
            sequences.init_sequence_patterns(self)

            # build database of int code <=> KEY_NAME
            self._keycodes = keyboard.get_keyboard_codes()

            # store attributes as: self.KEY_NAME = code
            for key_code, key_name in self._keycodes.items():
                setattr(self, key_name, key_code)

            # build database of sequence <=> KEY_NAME
            self._keymap = keyboard.get_keyboard_sequences(self)

        self._keyboard_buf = collections.deque()
        locale.setlocale(locale.LC_ALL, '')
        self._encoding = locale.getpreferredencoding()
        self._keyboard_decoder = codecs.getincrementaldecoder(self._encoding)()

        self.stream = stream

    #: Sugary names for commonly-used capabilities
    _sugar = dict(
        save='sc',
        restore='rc',
        # 'clear' clears the whole screen.
        clear_eol='el',
        clear_bol='el1',
        clear_eos='ed',
        position='cup',  # deprecated
        enter_fullscreen='smcup',
        exit_fullscreen='rmcup',
        move='cup',
        move_x='hpa',
        move_y='vpa',
        move_left='cub1',
        move_right='cuf1',
        move_up='cuu1',
        move_down='cud1',
        hide_cursor='civis',
        normal_cursor='cnorm',
        reset_colors='op',  # oc doesn't work on my OS X terminal.
        normal='sgr0',
        reverse='rev',
        italic='sitm',
        no_italic='ritm',
        shadow='sshm',
        no_shadow='rshm',
        standout='smso',
        no_standout='rmso',
        subscript='ssubm',
        no_subscript='rsubm',
        superscript='ssupm',
        no_superscript='rsupm',
        underline='smul',
        no_underline='rmul')

    def __getattr__(self, attr):
        """Return a terminal capability as Unicode string.

        For example, ``term.bold`` is a unicode string that may be prepended
        to text to set the video attribute for bold, which should also be
        terminated with the pairing ``term.normal``.

        This capability is also callable, so you can use ``term.bold("hi")``
        which results in the joining of (term.bold, "hi", term.normal).

        Compound formatters may also be used, for example:
        ``term.bold_blink_red_on_green("merry x-mas!")``.

        For a parametrized capability such as ``cup`` (cursor_address), pass
        the parameters as arguments ``some_term.cup(line, column)``. See
        manual page terminfo(5) for a complete list of capabilities.
        """
        if not self.does_styling:
            return formatters.NullCallableString()
        val = formatters.resolve_attribute(self, attr)
        # Cache capability codes.
        if not attr in dir(self):
            setattr(self, attr, val)
        return val

    @property
    def does_styling(self):
        """Whether attempt to emit capabilities

        This is influenced by the ``is_a_tty`` property and by the
        ``force_styling`` argument to the constructor. You can examine
        this value to decide whether to draw progress bars or other frippery.

        """
        return self._does_styling

    @property
    def is_a_tty(self):
        """Whether my ``stream`` appears to be associated with a terminal"""
        return self._is_a_tty

    @property
    def height(self):
        """T.height -> int

        The height of the terminal in characters.

        If an alternative ``stream`` is chosen, the size of that stream
        is returned if it is a connected to a terminal such as a pty.
        Otherwise, the size of the controlling terminal is returned.

        If neither of these streams are terminals, such as when stdout is piped
        to less(1), the values of the environment variable LINES and COLS are
        returned.

        None may be returned if no suitable size is discovered.
        """
        return self._height_and_width().ws_row

    @property
    def width(self):
        """T.width -> int

        The width of the terminal in characters.

        None may be returned if no suitable size is discovered.
        """
        return self._height_and_width().ws_col

    @staticmethod
    def _winsize(fd):
        """T._winsize -> WINSZ(ws_row, ws_col, ws_xpixel, ws_ypixel)

        The tty connected by file desriptor fd is queried for its window size,
        and returned as a collections.namedtuple instance WINSZ.

        May raise exception IOError.
        """
        data = fcntl.ioctl(fd, termios.TIOCGWINSZ, WINSZ._BUF)
        return WINSZ(*struct.unpack(WINSZ._FMT, data))

    def _height_and_width(self):
        """Return a tuple of (terminal height, terminal width).
        """
        # TODO(jquast): hey kids, even if stdout is redirected to a file,
        # we can still query sys.__stdin__.fileno() for our terminal size.
        # -- of course, if both are redirected, we have no use for this fd.
        for fd in (self._init_descriptor, sys.__stdout__):
            try:
                if fd is not None:
                    return self._winsize(fd)
            except IOError:
                pass

        return WINSZ(ws_row=int(os.getenv('LINES', '25')),
                     ws_col=int(os.getenv('COLUMNS', '80')),
                     ws_xpixel=None,
                     ws_ypixel=None)

    @contextlib.contextmanager
    def location(self, x=None, y=None):
        """Return a context manager for temporarily moving the cursor.

        Move the cursor to a certain position on entry, let you print stuff
        there, then return the cursor to its original position::

            term = Terminal()
            with term.location(2, 5):
                print 'Hello, world!'
                for x in xrange(10):
                    print 'I can do it %i times!' % x

        Specify ``x`` to move to a certain column, ``y`` to move to a certain
        row, both, or neither. If you specify neither, only the saving and
        restoration of cursor position will happen. This can be useful if you
        simply want to restore your place after doing some manual cursor
        movement.

        """
        # Save position and move to the requested column, row, or both:
        self.stream.write(self.save)
        if x is not None and y is not None:
            self.stream.write(self.move(y, x))
        elif x is not None:
            self.stream.write(self.move_x(x))
        elif y is not None:
            self.stream.write(self.move_y(y))
        try:
            yield
        finally:
            # Restore original cursor position:
            self.stream.write(self.restore)

    @contextlib.contextmanager
    def fullscreen(self):
        """Return a context manager that enters fullscreen mode while inside it
        and restores normal mode on leaving."""
        self.stream.write(self.enter_fullscreen)
        try:
            yield
        finally:
            self.stream.write(self.exit_fullscreen)

    @contextlib.contextmanager
    def hidden_cursor(self):
        """Return a context manager that hides the cursor while inside it and
        makes it visible on leaving."""
        self.stream.write(self.hide_cursor)
        try:
            yield
        finally:
            self.stream.write(self.normal_cursor)

    @property
    def color(self):
        """Return a capability that sets the foreground color.

        The capability is unparameterized until called and passed a number
        (0-15), at which point it returns another string which represents a
        specific color change. This second string can further be called to
        color a piece of text and set everything back to normal afterward.

        :arg num: The number, 0-15, of the color

        """
        if not self.does_styling:
            return formatters.NullCallableString()
        return formatters.ParameterizingString(
            self._foreground_color, self.normal)

    @property
    def on_color(self):
        """Return a capability that sets the background color.

        See ``color()``.

        """
        if not self.does_styling:
            return formatters.NullCallableString()
        return formatters.ParameterizingString(
            self._background_color, self.normal)

    @property
    def normal(self):
        """Return capability that resets video attribute.
        """
        if '_normal' in dir(self):
            return self._normal
        self._normal = formatters.resolve_capability(self, 'normal')
        return self._normal

    @property
    def number_of_colors(self):
        """Return the number of colors the terminal supports.

        Common values are 0, 8, 16, 88, and 256.

        Though the underlying capability returns -1 when there is no color
        support, we return 0. This lets you test more Pythonically::

            if term.number_of_colors:
                ...

        We also return 0 if the terminal won't tell us how many colors it
        supports, which I think is rare.

        """
        # This is actually the only remotely useful numeric capability. We
        # don't name it after the underlying capability, because we deviate
        # slightly from its behavior, and we might someday wish to give direct
        # access to it.
        #
        # trim value to 0, as tigetnum('colors') returns -1 if no support,
        # -2 if no such capability.
        return max(0, self.does_styling and curses.tigetnum('colors') or -1)

    @property
    def _foreground_color(self):
        return self.setaf or self.setf

    @property
    def _background_color(self):
        return self.setab or self.setb

    def ljust(self, text, width=None, fillchar=u' '):
        """T.ljust(text, [width], [fillchar]) -> string

        Return string ``text``, left-justified by printable length ``width``.
        Padding is done using the specified fill character (default is a
        space).  Default width is the attached terminal's width. ``text`` is
        escape-sequence safe."""
        if width is None:
            width = self.width
        return sequences.Sequence(text, self).ljust(width, fillchar)

    def rjust(self, text, width=None, fillchar=u' '):
        """T.rjust(text, [width], [fillchar]) -> string

        Return string ``text``, right-justified by printable length ``width``.
        Padding is done using the specified fill character (default is a space)
        Default width is the attached terminal's width. ``text`` is
        escape-sequence safe."""
        if width is None:
            width = self.width
        return sequences.Sequence(text, self).rjust(width, fillchar)

    def center(self, text, width=None, fillchar=u' '):
        """T.center(text, [width], [fillchar]) -> string

        Return string ``text``, centered by printable length ``width``.
        Padding is done using the specified fill character (default is a
        space).  Default width is the attached terminal's width. ``text`` is
        escape-sequence safe."""
        if width is None:
            width = self.width
        return sequences.Sequence(text, self).center(width, fillchar)

    def length(self, text):
        """T.length(text) -> int

        Return printable length of string ``text``, which may contain (some
        kinds) of sequences. Strings containing sequences such as 'clear',
        which repositions the cursor will not give accurate results.
        """
        return sequences.Sequence(text, self).length()

    def wrap(self, text, width=None, **kwargs):
        """T.wrap(text, [width=None, indent=u'', ...]) -> unicode

        Wrap paragraphs containing escape sequences, ``text``, to the full
        width of Terminal instance T, unless width is specified, wrapped by
        the virtual printable length, irregardless of the video attribute
        sequences it may contain.

        Returns a list of strings that may contain escape sequences. See
        textwrap.TextWrapper class for available additional kwargs to
        customize wrapping behavior.

        Note that the keyword argument ``break_long_words`` may not be set,
        it is not sequence-safe.
        """

        _blw = 'break_long_words'
        assert (_blw not in kwargs or not kwargs[_blw]), (
            "keyword argument, '{}' is not sequence-safe".format(_blw))

        width = width is None and self.width or width
        lines = []
        for line in text.splitlines():
            lines.extend(
                (_linewrap for _linewrap in sequences.SequenceTextWrapper(
                    width=width, term=self, **kwargs).wrap(text))
                if line.strip() else (u'',))

        return lines

    def kbhit(self, timeout=0):
        """T.kbhit([timeout=0]) -> bool

        Returns True if a keypress has been detected on keyboard.

        When ``timeout`` is 0, this call is non-blocking(default), or blocking
        indefinitely until keypress when ``None``, and blocking until keypress
        or time elapsed when ``timeout`` is non-zero.

        If input is not a terminal, False is always returned.
        """
        if self.keyboard_fd is None:
            return False

        check_r, check_w, check_x = [self.stream_kb], [], []
        ready_r, ready_w, ready_x = select.select(
            check_r, check_w, check_x, timeout)

        return check_r == ready_r

    @contextlib.contextmanager
    def cbreak(self):
        """Return a context manager that enters 'cbreak' mode: disabling line
        buffering of keyboard input, making characters typed by the user
        immediately available to the program.  Also referred to as 'rare'
        mode, this is the opposite of 'cooked' mode, the default for most
        shells.

        In 'cbreak' mode, echo of input is also disabled: the application must
        explicitly print any input received, if they so wish.

        More information can be found in the manual page for curses.h,
           http://www.openbsd.org/cgi-bin/man.cgi?query=cbreak

        The python manual for curses,
           http://docs.python.org/2/library/curses.html

        Note also that setcbreak sets VMIN = 1 and VTIME = 0,
           http://www.unixwiz.net/techtips/termios-vmin-vtime.html
        """
        assert self.is_a_tty, 'stream is not a tty.'
        if self.stream_kb is not None:
            # save current terminal mode,
            save_mode = termios.tcgetattr(self.stream_kb)
            tty.setcbreak(self.stream_kb, termios.TCSANOW)
            try:
                yield
            finally:
                # restore prior mode,
                termios.tcsetattr(self.stream_kb, termios.TCSAFLUSH, save_mode)
        else:
            yield

    @contextlib.contextmanager
    def raw(self):
        """Return a context manager that enters 'raw' mode. Raw mode is
        similar to cbreak mode, in that characters typed are immediately passed
        through to the user program. The differences are that in raw mode, the
        interrupt, quit, suspend, and flow control characters are all passed
        through uninterpreted, instead of generating a signal.
        """
        assert self.is_a_tty, 'stream is not a tty.'
        if self.stream_kb is not None:
            # save current terminal mode,
            save_mode = termios.tcgetattr(self.stream_kb)
            tty.setraw(self.stream_kb, termios.TCSANOW)
            try:
                yield
            finally:
                # restore prior mode,
                termios.tcsetattr(self.stream_kb, termios.TCSAFLUSH, save_mode)
        else:
            yield

    def inkey(self, timeout=None, esc_delay=0.35):
        """T.inkey(timeout=None, esc_delay=0.35) -> Keypress()

        Receive next keystroke from keyboard (stdin), blocking until a
        keypress is received or ``timeout`` elapsed, if specified.

        When used without the context manager ``cbreak``, stdin remains
        line-buffered, and this function will block until return is pressed.

        The value returned is an instance of ``Keystroke``, with properties
        ``is_sequence``, and, when True, non-None values for ``code`` and
        ``name``. The value of ``code`` may be compared against attributes
        of this terminal beginning with KEY, such as KEY_ESCAPE.

        To distinguish between KEY_ESCAPE, and sequences beginning with
        escape, the ``esc_delay`` specifies the amount of time after receiving
        the escape character ('\x1b') to seek for application keys.

        """
        # TODO(jquast): "meta sends escape", where alt+1 would send '\x1b1',
        #               what do we do with that? Surely, something useful.
        #               comparator to term.KEY_meta('x') ?
        # TODO(jquast): Ctrl characters, KEY_CTRL_[A-Z], and the rest;
        #               KEY_CTRL_\, KEY_CTRL_{, etc. are not legitimate
        #               attributes. comparator to term.KEY_ctrl('z') ?
        def _timeleft(stime, timeout):
            """_timeleft(stime, timeout) -> float

            Returns time-relative time remaining before ``timeout`` after time
            elapsed since ``stime``.
            """
            if timeout is not None:
                if timeout is 0:
                    return 0
                return max(0, timeout - (time.time() - stime))

        def _decode_next():
            """Read and decode next byte from stdin."""
            byte = os.read(self.stream_kb, 1)
            return self._keyboard_decoder.decode(byte, final=False)

        resolve = functools.partial(keyboard.resolve_sequence,
                                    mapper=self._keymap,
                                    codes=self._keycodes)

        stime = time.time()

        # re-buffer previously received keystrokes,
        ucs = u''
        while self._keyboard_buf:
            ucs += self._keyboard_buf.pop()

        # receive all immediately available bytes
        while self.kbhit():
            ucs += _decode_next()

        # decode keystroke, if any
        ks = resolve(text=ucs)

        # so long as the most immediately received or buffered keystroke is
        # incomplete, (which may be a multibyte encoding), block until until
        # one is received.
        while not ks and self.kbhit(_timeleft(stime, timeout)):
            ucs += _decode_next()
            ks = resolve(text=ucs)

        # handle escape key (KEY_ESCAPE) vs. escape sequence (which begins
        # with KEY_ESCAPE, \x1b[, \x1bO, or \x1b?), up to esc_delay when
        # received. This is not optimal, but causes least delay when
        # (currently unhandled, and rare) "meta sends escape" is used,
        # or when an unsupported sequence is sent.
        if ks.code is self.KEY_ESCAPE:
            esctime = time.time()
            while (ks.code is self.KEY_ESCAPE and
                   self.kbhit(_timeleft(esctime, esc_delay))):
                ucs += _decode_next()
                ks = resolve(text=ucs)

        # buffer any remaining text received
        self._keyboard_buf.extendleft(ucs[len(ks):])
        return ks

# From libcurses/doc/ncurses-intro.html (ESR, Thomas Dickey, et. al):
#
#   "After the call to setupterm(), the global variable cur_term is set to
#    point to the current structure of terminal capabilities. By calling
#    setupterm() for each terminal, and saving and restoring cur_term, it
#    is possible for a program to use two or more terminals at once."
#
# However, if you study Python's ./Modules/_cursesmodule.c, you'll find:
#
#   if (!initialised_setupterm && setupterm(termstr,fd,&err) == ERR) {
#
# Python - perhaps wrongly - will not allow a re-initialisation of new
# terminals through setupterm(), so the value of cur_term cannot be changed
# once set: subsequent calls to setupterm() have no effect.
#
# Therefore, the ``kind`` of each Terminal() is, in essence, a singleton.
# This global variable reflects that, and a warning is emitted if somebody
# expects otherwise.

_CUR_TERM = None

WINSZ = collections.namedtuple('WINSZ', (
    'ws_row',     # /* rows, in characters */
    'ws_col',     # /* columns, in characters */
    'ws_xpixel',  # /* horizontal size, pixels */
    'ws_ypixel',  # /* vertical size, pixels */
))
#: format of termios structure
WINSZ._FMT = 'hhhh'
#: buffer of termios structure appropriate for ioctl argument
WINSZ._BUF = '\x00' * struct.calcsize(WINSZ._FMT)
