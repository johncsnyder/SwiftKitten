import sys
import os
import time
import re
import functools
import logging
import json
import uuid
import subprocess
import pickle
import shlex
from subprocess import STDOUT, check_output, TimeoutExpired
from subprocess import Popen, PIPE
import threading
import sublime
import sublime_plugin
from sublime import load_settings, set_timeout_async, Region, DRAW_EMPTY
from sublime import INHIBIT_WORD_COMPLETIONS, INHIBIT_EXPLICIT_COMPLETIONS
import pygments
import pygments.lexers
from pygments.lexers import SwiftLexer
import xml
import xml.etree
from xml.etree import ElementTree as ET

# dependency paths
package_path = os.path.dirname(__file__)
packages = ["ijson","cffi","pycparser"]
paths = [os.path.join(package_path, package) for package in packages]

# add paths
for path in paths:
    if path not in sys.path:
        sys.path.append(path)

# import dependencies
try:
    # fast yajl backend
    import ijson.backends.yajl2_cffi as ijson
except:
    # pure python backend
    logging.warning("Failed to import yajl2_cffi backend for ijson.")
    import ijson



# check Sublime version
if sys.version_info < (3, 3):
    raise ImportError("SwiftKitten requires Sublime Text 3")



def plugin_loaded():
    """Called directly from sublime on plugin load"""
    SwiftKittenEventListener._load_framework_cache()
    pass


#def plugin_unloaded():
#    """Called directly from sublime on plugin unload"""
#    pass



class AutocompleteRequestError(RuntimeError):
    def __init__(self,*args,**kwargs):
        RuntimeError.__init__(self,*args,**kwargs)



class SwiftKittenEventListener(sublime_plugin.EventListener):
    """
    """

    # regexes for formatting function args in completion request
    prog = re.compile(r"<#T##(.+?)#>")
    arg_prog = re.compile(r"##.+")

    # cache of completion data
    cache = {}
    framework_cache = {}

    # id of current completion query
    query_id = None

    # number of concurrent completion requests
    current_requests = set()

    # linting
    errors = {}

    # idle parameters
    delay = 300
    pending = 0

    # pygments Swift language parser
    lexer = SwiftLexer()


    def __init__(self):
        """
        """
        super(SwiftKittenEventListener, self).__init__()
        SwiftKittenEventListener.shared_instance = self


    def handle_timeout(self, view):
        """
        """
        self.pending -= 1
        if self.pending == 0:
            self.on_idle(view)


    def on_idle(self, view):
        """
        """
        structure_info = self._get_structure_info(view)
        linting = self.get_settings(view, "linting", True)

        # linting
        if linting and "key.diagnostics" in structure_info:
            diagnostics = structure_info["key.diagnostics"]
            self.errors = {}

            for entry in diagnostics:
                description = entry["key.description"]
                #level = entry['key.severity']
                row, col = entry["key.line"], entry["key.column"]
                pos = view.text_point(row-1,col-1)

                self.errors[pos] = description

            view.add_regions(
                "swiftkitten.diagnostics",
                [Region(pos,pos+1) for pos in self.errors.keys()],
                "constant",
                "",
                sublime.DRAW_STIPPLED_UNDERLINE | sublime.DRAW_NO_OUTLINE | sublime.DRAW_NO_FILL
            )

            self._update_linting_status(view)


    def on_modified(self, view):
        """
        """
        sel = view.sel()
        if not view.match_selector(sel[0].a, "source.swift"):
            return

        # clear linting
        self.errors = {}
        view.erase_regions("swiftkitten.diagnostics")

        self.query_id = None
        self.pending += 1

        def handle_timeout():
            self.handle_timeout(view)
        sublime.set_timeout_async(handle_timeout, self.delay)


    def on_post_save_async(self, view):
        """
        """
        sel = view.sel()
        if not view.match_selector(sel[0].a, "source.swift"):
            return

        self._save_framework_cache()


    def _update_linting_status(self, view):
        """
        """
        sel = view.sel()
        pos = sel[0].a

        if pos in self.errors:
            view.set_status("swiftkitten.diagnostics", self.errors[pos])
        else:
            view.erase_status("swiftkitten.diagnostics")


    def on_selection_modified(self, view):
        """
        """
        sel = view.sel()
        if not view.match_selector(sel[0].a, "source.swift"):
            return

        self._update_linting_status(view)
        self.query_id = None


    def get_completion_flags(self, view):
        """Get Sublime completion flags from user settings.
        """
        cpflags = False

        if self.get_settings(view, "suppress_word_completions", False):
            cpflags = INHIBIT_WORD_COMPLETIONS

        if self.get_settings(view, "suppress_explicit_completions", False):
            cpflags |= INHIBIT_EXPLICIT_COMPLETIONS

        return cpflags


    def get_compilerargs(self, view):
        """Get compiler arguments for SourceKitten command.
        """
        sdk = self.get_settings(view, "sdk")
        frameworks_paths = self.get_settings(view, "extra_framework_paths")
        compilerargs = "-sdk " + sdk if sdk != "" else ""
        compilerargs += " ".join(("-F " + path for path in frameworks_paths))
        compilerargs += " " + self.get_settings(view, "extra_compilerargs")
        if compilerargs == " ":
            return ""
        return compilerargs


    def _format_match(self, index, match):
        """
        """
        index[0] += 1
        arg = self.arg_prog.sub("", match.group(1))
        return "${%s:%s}" % (index[0], arg)


    def _format_snippet(self, text):
        """
        """
        index = [0]
        snippet = self.prog.sub(functools.partial(self._format_match, index), text)
        return snippet


    def _format_completion(self, entry):
        """
        """
        description = entry["descriptionKey"]
        hint = entry["docBrief"] if "docBrief" in entry else entry["typeName"]
        snippet = self._format_snippet(entry["sourcetext"]).strip(".")
        #snippet = json.dumps(entry)
        return [description + '\t' + hint, snippet]


    @classmethod
    def _get_cache_path(cls):
        """
        """
        return os.path.join(sublime.cache_path(), "SwiftKitten")


    @classmethod
    def _load_framework_cache(cls):
        """
        """
        framework_cache_path = os.path.join(cls._get_cache_path(), "frameworks.cache")

        if os.path.exists(framework_cache_path):
            with open(framework_cache_path, "rb") as f:
                cls.framework_cache = pickle.load(f)


    @classmethod
    def _save_framework_cache(cls):
        """
        """
        cache_path = cls._get_cache_path()

        if not os.path.exists(cache_path):
            os.mkdir(cache_path)

        framework_cache_path = os.path.join(cache_path, "frameworks.cache")

        with open(framework_cache_path, "wb") as f:
            pickle.dump(cls.framework_cache, f)


    @classmethod
    def get_settings(self, view, key, default=None):
        """Get user settings for key.

        Combine SwiftKitten package settings with project settings
        """
        settings = load_settings("SwiftKitten.sublime-settings")
        project_data = view.window().project_data()
        return project_data.get(key, settings.get(key, default))


    def get_completion_cmd(self, view, text, offset):
        """Get completion command.
        """
        cmd = "{sourcekitten_binary} complete --text {text} --offset {offset} -- {compilerargs}"
        sourcekitten_binary = self.get_settings(view,
            "sourcekitten_binary", "sourcekitten")
        compilerargs = self.get_compilerargs(view)
        return cmd.format(
            sourcekitten_binary=sourcekitten_binary,
            text=shlex.quote(text),
            offset=offset,
            compilerargs=shlex.quote(compilerargs)
        )


    def get_structure_info_cmd(self, view, text):
        """Get structure info command.
        """
        cmd = "{sourcekitten_binary} structure --text {text}"
        sourcekitten_binary = self.get_settings(view,
            "sourcekitten_binary", "sourcekitten")
        return cmd.format(
            sourcekitten_binary=sourcekitten_binary,
            text=shlex.quote(text)
        )


    def _get_structure_info(self, view):
        """
        """
         #get structure info command
        text = view.substr(Region(0, view.size()))
        cmd = self.get_structure_info_cmd(view, text)
        timeout = self.get_settings(view, "sourcekitten_timeout", 1.0)

        # run structure info command
        p = Popen(cmd, shell=True, stdout=PIPE, stderr=STDOUT)
        structure_info = list(ijson.items(p.stdout,''))[0]

        return structure_info


    def _parse_completions(self, parser, included=lambda item: True):
        """Parse and format completion data from a ijson parser.
        """
        item = None
        item_ids = set()
        for prefix, event, value in parser:
            if event == "start_map":
                item = {}
            elif event == "end_map":
                # exclude duplicates
                item_id = item.get("associatedUSRs", item["name"])
                if included(item) and item_id not in item_ids:
                    item_ids.add(item_id)
                    # yield formatted completion
                    yield self._format_completion(item)
            elif event == "map_key":
                item[value] = next(parser)[2]


    def _autocomplete_request(self, view, cache, request,
            text, offset, included=lambda item: True):
        """
        """
        # this should not happen, but just in case, do not
        # overload the system with too many requests
        if len(self.current_requests) > self.get_settings(view, "concurrent_request_limit", 4):
            raise AutocompleteRequestError("Request denied: too many concurrent requests.")

        # prevent duplicate requests
        if request in self.current_requests:
            raise AutocompleteRequestError(
                "Request denied: completion for \"{request}\" "
                "already in progress.".format(request=request)
            )

        # start request
        self.current_requests.add(request)

        # get completion command
        cmd = self.get_completion_cmd(view, text, offset)

        # run completion command
        p = Popen(cmd, shell=True, stdout=PIPE, stderr=STDOUT)
        parser = ijson.parse(p.stdout)
        completions = list(self._parse_completions(parser, included=included))

        # finish request
        self.current_requests.discard(request)

        return completions


    def _autocomplete_framework(self, view, framework):
        """
        """
        try:
            text = "import " + framework + "; "

            def included(item):
                return item["context"] == "source.codecompletion.context.othermodule" and \
                       item["moduleName"] != "Swift"

            completions = self._autocomplete_request(view, self.framework_cache,
                "."+framework, text, len(text), included=included)

        except AutocompleteRequestError as e:
            print(e)

        else:
            self.framework_cache[framework] = completions


    def _autocomplete(self, view, text, offset, stub, query_id):
        """Request autocomplete data from SourceKitten.
        """
        buffer_id = view.buffer_id()
        cache = self.cache[buffer_id]
        sel = view.sel()

        try:
            completions = self._autocomplete_request(view, cache,
                stub, text, offset)

        except AutocompleteRequestError as e:
            print(e)

        else:
            # update cache timestamp if nothing has changed
            if stub in cache and completions == cache[stub]["completions"]:
                cache[stub]["timestamp"] = time.time()
                return

            # cache completions for this buffer associated with stub
            cache[stub] = {
                "completions" : completions,
                "timestamp"   : time.time()
            }

            # update completions if in the autocomplete window still open
            if self.query_id == query_id:
                view.run_command("hide_auto_complete")
                view.run_command("auto_complete", {
                    "disable_auto_insert": True,
                    "api_completions_only": False,
                    "next_completion_if_showing": False,
                    "auto_complete_commit_on_tab": True,
                })


    def _serialize_token(self, pair):
        """Get string representation of (token, value) pair.
        """
        from pygments.token import Token
        token, value = pair
        # for literals, autocomplete only depends
        # on type of argument, not the value
        if token in [Token.Literal.Number.Float,
                     Token.Literal.Number.Integer,
                     Token.Literal.String]:
            return str(token)
        else:
            return value


    def _autocomplete_async(self, view, text, offset, stub, query_id):
        """
        """
        # curry autocomplete request with query data
        _autocomplete = functools.partial(self._autocomplete, view, text, offset, stub, query_id)
        worker = threading.Thread(target=_autocomplete).start()


    def _autocomplete_framework_async(self, view, framework):
        """
        """
        # curry autocomplete request with query data
        _autocomplete_framework = functools.partial(self._autocomplete_framework, view, framework)
        worker = threading.Thread(target=_autocomplete_framework).start()


    def _extract_frameworks(self, view, text):
        """Extract import framework statements from text, replacing with whitespace.
        """
        frameworks = []

        def repl(match):
            if view.score_selector(match.start(), "keyword.other.import.swift"):
                frameworks.append(match.group(2))
                return " " * len(match.group(0))
            else:
                return match.group(0)

        text = re.sub("import(\s+)(\w+)", repl, text)
        return frameworks, text


    def _match_prefix(self, prefix, item):
        """
        """
        return item[0].startswith(prefix)


    def on_query_completions(self, view, prefix, locations):
        """Sublime autocomplete query.
        """
        buffer_id = view.buffer_id()
        sel = view.sel()
        pos = sel[0].a

        if not view.match_selector(pos, "source.swift"):
            return

        # the offset in completion requests in sourcekitten
        # must be made at the start of postfix '.'
        offset = pos - len(prefix)

        if buffer_id not in self.cache:
            self.cache[buffer_id] = {}  # initalize cache for buffer

        # create a unique id for this autocomplete request
        self.query_id = str(uuid.uuid1())

        # parse stub, for example:
        #   foo.         -> foo
        #   foo(bar).baz -> foo(baz)
        #   (foo + bar). -> (foo + bar)
        text = view.substr(Region(0, offset))
        stub = get_autocomplete_stub(self.lexer, text)

        # serialize stub
        stub = "".join(map(self._serialize_token, stub))

        # initalize completion info
        completions = []
        cpflags = self.get_completion_flags(view)

        # remove import framework statements if stub is empty
        # and extract framework names. global variables imported
        # from frameworks are stored in a separate cache
        if stub == "":
            excluded_frameworks = self.get_settings(view, "exclude_framework_globals", [])
            match_prefix = functools.partial(self._match_prefix, prefix)
            frameworks, text = self._extract_frameworks(view, text)

            for framework in frameworks:
                if framework not in excluded_frameworks:

                    if framework in self.framework_cache:
                        # disable fuzzy matching for globals
                        completions += filter(match_prefix, self.framework_cache[framework])
                    else:
                        self._autocomplete_framework_async(view, framework)

        # check if stub is cached
        if stub in self.cache[buffer_id]:
            completions += self.cache[buffer_id][stub]["completions"]

            # check timestamp
            now = time.time()
            timestamp = self.cache[buffer_id][stub]["timestamp"]
            cache_timeout = self.get_settings(view, "cache_timeout", 1.0)

            # if cached completion data still valid, do not make request
            if (now - timestamp) > cache_timeout:
                self._autocomplete_async(view, text, offset, stub, self.query_id)

        else:
            # request completions
            self._autocomplete_async(view, text, offset, stub, self.query_id)

        # return completions
        return (completions, cpflags) if cpflags else completions





def get_tokens_reversed(lexer, text):
    """
    """
    lines = text.splitlines()
    for line in lines[::-1]:
        tokens = reversed(list(lexer.get_tokens(line)))
        yield from tokens



def get_autocomplete_stub(lexer, text):
    """
    """
    entity = []

    from pygments.token import Token

    # ignored tokens
    ignored = [Token.Comment, Token.Text, Token.Text.Whitespace, Token.Comment.Single]
    filtered = lambda pair: pair[0] not in ignored  # pair = (token,value)

    tokens = filter(filtered, get_tokens_reversed(lexer, text))
    blocks = get_blocks(tokens)
    block = next(blocks, [])

    if len(block) == 1 and block[0][1] == ".":
        block = next(blocks, [])

        if len(block) > 0 and block[0][1] == "(":
            block_ = next(blocks, [])

            if len(block_) == 1 and block[0][0] is Token.Name:
                return block_ + block

        return block

    return []



def get_blocks(tokens):
    """
    """
    block = []
    level = 0

    from pygments.token import Token

    for token, value in tokens:
        block.append((token,value))

        if value == ")":
            level += 1
        elif value == "(":
            level -= 1

        if level == 0:
            yield block[::-1]
            block = []






class swift_kitten_clear_cache_command(sublime_plugin.TextCommand):

    def run(self, edit):
        """Manually clear completion cache.
        """
        SwiftKittenEventListener.cache = {}
        SwiftKittenEventListener.framework_cache = {}





class swift_kitten_display_documentation_command(sublime_plugin.TextCommand):

    xml_to_html_tags = {
        "Name"              : "h3",
        "Abstract"          : "div",
        "Protocol"          : "div",
        "InstanceMethod"    : "div",
        "SeeAlsos"          : "div",
        "Discussion"        : "div",
        "Declaration"       : "div",
        "Class"             : "div",
        "Note"              : "div",
        "Para"              : "p",
        "SeeAlso"           : "p",
        "Availability"      : "p",
        "zModuleImport"     : "p",
        "zAttributes"       : "p",
        "Attribute"         : "p",
        "uAPI"              : "a",
        "codeVoice"         : "pre",
        "newTerm"           : "em",
        "List-Bullet"       : "ul",
        "Item"              : "li",
        "AvailabilityItem"  : "em",
        "InstanceProperty"  : "div",
        "CodeListing"       : "div",
        "reservedWord"      : "b",
        "keyWord"           : "span",
        "Property-ObjC"     : "div",
        "kConstantName"     : "em",
        "Structure"         : "div",
        "Function"          : "div",
        "ClassMethod"       : "div",
        "ClassProperty"     : "div",
        "Enumeration"       : "div",
        "Constant"          : "div"
    }


    def get_docsetutil_cmd(self, view, docset, query):
        """
        """
        docsetutil_binary = SwiftKittenEventListener.get_settings(view, "docsetutil_binary")
        return ["/usr/local/bin/docsetutil", "search", "-skip-text", "-query", query, docset]


    def get_tokens_path(self, docset):
        """
        """
        return os.path.join(docset, "Contents/Resources/Tokens")


    def convert_docs_to_html(self, xml):
        """
        """
        root = ET.fromstring(xml)

        for el in root.iter():
            el.tag = self.xml_to_html_tags.get(el.tag, el.tag)

            if el.tag == "a":
                if "url" in el.attrib:
                    el.attrib["href"] = el.attrib["url"]
                    del el.attrib["url"]

        return ET.tostring(root, encoding="utf-8")


    def run(self, edit):
        """
        """
        view = self.view
        sel = view.sel()

        if len(sel) == 0:
            return

        a,b = sel[0]
        query = view.substr(view.word(a)) if a == b else view.substr(sel[0])

        if query == "":
            return

        # run docsetutil command
        docset = SwiftKittenEventListener.get_settings(view, "docset")
        cmd = self.get_docsetutil_cmd(view, docset, query)
        results = check_output(cmd, stderr=STDOUT)
        results = str(results, 'utf-8')

        if len(results) == 0:
            print("No documentation found.")
            return

        lines = results.splitlines()

        # split each line into two paths
        pairs = map(lambda line: line.strip().split("   "), lines)

        get_lang = lambda a: a.split('/')[0]
        get_path = lambda a,b: os.path.join(os.path.dirname(a),
            os.path.basename(b))

        #
        docs = {get_lang(a) : get_path(a,b) for a,b in pairs}

        # prefer Swift, Objective-C, C
        lang = sorted(docs.keys())[-1]

        # construct path to documentation token
        path = os.path.join(self.get_tokens_path(docset), docs[lang] + ".xml")

        # read documentation file
        with open(path, "rb") as f:
            xml = f.read()

        # convert xml to html
        html = str(self.convert_docs_to_html(xml), "utf-8")

        #
        # TO DO:
        # add on_navigate handler
        #

        # display documentation
        view.show_popup(html, max_width=400, max_height=600)



