    ## $Id$
## Administrator interface for WebAccess

## This file is part of CDS Invenio.
## Copyright (C) 2002, 2003, 2004, 2005, 2006, 2007 CERN.
##
## CDS Invenio is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 2 of the
## License, or (at your option) any later version.
##
## CDS Invenio is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with CDS Invenio; if not, write to the Free Software Foundation, Inc.,
## 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

"""CDS Invenio Access Control FireRole."""

__revision__ = "$Id$"

__lastupdated__ = """$Date$"""

"""These functions are for realizing a firewall like role definition for extending
webaccess to connect user to roles using every infos about users.
"""

from invenio.webgroup_dblayer import get_groups
from invenio.webinterface_handler import http_get_credentials
from invenio.access_control_config import WebAccessFireroleError
from invenio.dbquery import run_sql
from invenio.access_control_config import CFG_ACC_EMPTY_ROLE_DEFINITION_SRC, \
        CFG_ACC_EMPTY_ROLE_DEFINITION_SER
from socket import gethostbyname
import re
import cPickle
from zlib import compress, decompress


# INTERFACE

def compile_role_definition(firerole_def_src):
    """ Given a text in which every row contains a rule it returns the compiled
    object definition.
    Rules have the following syntax:
    allow|deny [not] field {list of one or more (double)quoted string or regexp}
    or allow|deny any
    Every row may contain a # sign followed by a comment which are discarded.
    Field could be any key contained in a user_info dictionary. If the key does
    not exist in the dictionary, the rule is skipped.
    The first rule which matches return.
    """
    line = 0
    ret = []
    default_allow_p = False
    if not firerole_def_src or not firerole_def_src.strip():
        firerole_def_src = CFG_ACC_EMPTY_ROLE_DEFINITION_SRC
    for row in firerole_def_src.split('\n'):
        line += 1
        row = row.strip()
        if not row:
            continue
        clean_row = _no_comment_re.sub('', row)
        if clean_row:
            g = _any_rule_re.match(clean_row)
            if g:
                default_allow_p = g.group('command').lower() == 'allow'
                break
            g = _rule_re.match(clean_row)
            if g:
                allow_p = g.group('command').lower() == 'allow'
                not_p = g.group('not') != None
                field = g.group('field').lower()
                # Renaming groups to group and apache_groups to apache_group
                for alias_item in _aliasTable:
                    if field in alias_item:
                        field = alias_item[0]
                        break
                expressions = g.group('expression')+g.group('more_expressions')
                expressions_list = []
                for expr in _expressions_re.finditer(expressions):
                    expr = expr.group()
                    if expr[0] == '/':
                        try:
                            expressions_list.append((True, re.compile(expr[1:-1], re.I)))
                        except Exception, msg:
                            raise WebAccessFireroleError, "Syntax error while compiling rule %s (line %s): %s is not a valid re because %s!" % (row, line, expr, msg)
                    else:
                        if field == 'remote_ip' and '/' in expr[1:-1]:
                            try:
                                expressions_list.append((False, _ip_matcher_builder(expr[1:-1])))
                            except Exception, msg:
                                raise WebAccessFireroleError, "Syntax error while compiling rule %s (line %s): %s is not a valid ip group because %s!" % (row, line, expr, msg)
                        else:
                            expressions_list.append((False, expr[1:-1]))
                expressions_list = tuple(expressions_list)
                ret.append((allow_p, not_p, field, expressions_list))
            else:
                raise WebAccessFireroleError, "Syntax error while compiling rule %s (line %s): not a valid rule!" % (row, line)
    return (compress(cPickle.dumps((default_allow_p, tuple(ret)), -1)))


def repair_role_definitions():
    """ Try to rebuild compiled serialized definitions from their respectives
    sources. This is needed in case Python break back compatibility.
    """
    definitions = run_sql("SELECT id, firerole_def_src FROM accROLE""")
    for role_id, firerole_def_src in definitions:
        run_sql("UPDATE accROLE SET firerole_def_ser=%s WHERE id=%s", (compile_role_definition(firerole_def_src), role_id))

def store_role_definition(role_id, firerole_def_ser, firerole_def_src):
    """ Store a compiled serialized definition and its source in the database
    alongside the role to which it belong.
    @param role_id the role_id
    @param firerole_def_ser the serialized compiled definition
    @param firerole_def_src the sources from which the definition was taken
    """
    run_sql("UPDATE accROLE SET firerole_def_ser=%s, firerole_def_src=%s WHERE id=%s", (firerole_def_ser, firerole_def_src, role_id))

def load_role_definition(role_id):
    """ Load the definition corresponding to a role. If the compiled definition
    is corrupted it try to repairs definitions from their sources and try again
    to return the definition.
    @param the role_id
    @return a deserialized compiled role definition
    """
    res = run_sql("SELECT firerole_def_ser FROM accROLE WHERE id=%s", (role_id, ), 1)
    if res:
        try:
            return cPickle.loads(decompress(res[0][0]))
        except Exception:
            repair_role_definitions()
            res = run_sql("SELECT firerole_def_ser FROM accROLE WHERE id=%s", (role_id, ), 1)
            if res:
                return cPickle.loads(decompress(res[0][0]))
            else:
                return (False, ())
    else:
        return (False, ())

def acc_firerole_check_user(user_info, firerole_def_obj):
    """ Given a user_info dictionary, it matches the rules inside the deserializez
    compiled definition in order to discover if the current user match the roles
    corresponding to this definition.
    @param user_info a dict produced by collect_user_info which contains every
    info about a user
    @param definition a compiled deserialized definition produced by compile_role_defintion
    @return True if the user match the definition, False otherwise.
    """
    try:
        default_allow_p, rules = firerole_def_obj
        for (allow_p, not_p, field, expressions_list) in rules: # for every rule
            group_p = field in ['group', 'apache_group'] # Is it related to group?
            ip_p = field == 'remote_ip' # Is it related to Ips?
            next_rule_p = False # Silly flag to break 2 for cycle
            if not user_info.has_key(field):
                continue
            for reg_p, expr in expressions_list: # For every element in the rule
                if group_p: # Special case: groups
                    if reg_p: # When it is a regexp
                        for group in user_info[field]: # iterate over every group
                            if expr.match(group): # if it matches
                                if not_p: # if must not match
                                    next_rule_p = True # let's skip to next rule
                                    break
                                else: # Ok!
                                    return allow_p
                        if next_rule_p:
                            break # I said: let's skip to next rule ;-)
                    elif expr.lower() in [group.lower() for group in user_info[field]]: # Simple expression then just check for expr in groups
                        if not_p: # If expr is in groups then if must not match
                            break # let's skip to next rule
                        else: # Ok!
                            return allow_p
                elif reg_p: # Not a group, then easier. If it's a regexp
                    if expr.match(user_info[field]): # if it matches
                        if not_p: # If must not match
                            break # Let's skip to next rule
                        else:
                            return allow_p # Ok!
                elif ip_p and type(expr) == type(()): # If it's just a simple expression but an IP!
                    if _ipmatch(user_info['remote_ip'], expr): # Then if Ip matches
                        if not_p: # If must not match
                            break # let's skip to next rule
                        else:
                            return allow_p # ok!
                elif expr.lower() == user_info[field].lower(): # Finally the easiest one!!
                    if not_p: # ...
                        break
                    else: # ...
                        return allow_p # ...
            if not_p and not next_rule_p: # Nothing has matched and we got not
                return allow_p # Then the whole rule matched!
    except Exception, msg:
        raise WebAccessFireroleError, msg
    return default_allow_p # By default we allow ;-) it'an OpenSource project

def deserialize(firerole_def_ser):
    """ Deserialize and decompress a definition."""
    if firerole_def_ser:
        return cPickle.loads(decompress(firerole_def_ser))
    else:
        return cPickle.loads(decompress(CFG_ACC_EMPTY_ROLE_DEFINITION_SER))

# IMPLEMENTATION

# Comment finder
_no_comment_re = re.compile(r'[\s]*(?<!\\)#.*')

# Rule dissecter
_rule_re = re.compile(r'(?P<command>allow|deny)[\s]+(?:(?P<not>not)[\s]+)?(?P<field>[\w]+)[\s]+(?P<expression>(?<!\\)\'.+?(?<!\\)\'|(?<!\\)\".+?(?<!\\)\"|(?<!\\)\/.+?(?<!\\)\/)(?P<more_expressions>([\s]*,[\s]*((?<!\\)\'.+?(?<!\\)\'|(?<!\\)\".+?(?<!\\)\"|(?<!\\)\/.+?(?<!\\)\/))*)(?:[\s]*(?<!\\).*)?', re.I)

_any_rule_re = re.compile(r'(?P<command>allow|deny)[\s]+any[\s]*', re.I)

# Sub expression finder
_expressions_re = re.compile(r'(?<!\\)\'.+?(?<!\\)\'|(?<!\\)\".+?(?<!\\)\"|(?<!\\)\/.+?(?<!\\)\/')

def _mkip (ip):
    """ Compute a numerical value for a dotted IP """
    num = 0L
    for i in map (int, ip.split ('.')): num = (num << 8) + i
    return num

_full = 2L ** 32 - 1


_aliasTable = (('group', 'groups'), ('apache_group', 'apache_groups'))


def _ip_matcher_builder(group):
    """ Compile a string "ip/bitmask" (i.e. 127.0.0.0/24)
    @param group a classical "ip/bitmask" string
    @return a tuple containing the gip and mask in a binary version.
    """
    gip, gmk = group.split('/')
    gip = _mkip(gip)
    gmk = int(gmk)
    mask = (_full - (2L ** (32 - gmk) - 1))
    if not (gip & mask == gip):
        raise WebAccessFireroleError, "Netmask does not match IP (%Lx %Lx)" % (gip, mask)
    return (gip, mask)

def _ipmatch(ip, ip_matcher):
    """ Check if an ip matches an ip_group.
    @param ip the ip to check
    @param ip_matcher a compiled ip_group produced by ip_matcher_builder
    @return True if ip matches, False otherwise
    """
    return _mkip(ip) & ip_matcher[1] == ip_matcher[0]

