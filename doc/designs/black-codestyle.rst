=============================
Format Python code with black
=============================

**WORK IN PROGRESS**

This design document proposed to adopt |black| as code style for |FreeIPA|'s
Python code and to enforce a consistent style by auto-formatting with |black|.

Overview
========

|black| is a code formatter for Python code. From `black project`_ page:

   black is the uncompromising Python code formatter. By using it, you agree
   to cede control over minutiae of hand-formatting. In return, Black gives
   you speed, determinism, and freedom from pycodestyle nagging about
   formatting. You will save time and mental energy for more important
   matters.

|black|'s formatting is safe and does never change meaning of code.
Internally the tool verifies that the reformatting code produces exactly the
same AST as the original code. An internal cache speeds up subsequent runs of
|black|.

The tool also has a check mode (``black --check``) for linting that does not
modify any code.

.. note::

   This design document is a complement of Django's `DEP 0008`_ document. It
   highlights and discusses special cases for |FreeIPA| or where |FreeIPA|
   deviates from `DEP 0008`_. Please read the document before continuing.

Used in notable open-source project
-----------------------------------

|black| has been successfully adopted by several large and prominent
Open Source projects. 

* pytest
* tox
* Pyramid
* Django
* Hypothesis
* attrs
* SQLAlchemy
* Poetry
* PyPA applications (Warehouse, Pipenv, virtualenv)
* pandas
* Pillow
* ... and many more


Benefits
========

The Django Enhancement Proposal `DEP 0008`_ explains the benefits of
automated code formatting and adoption of |black| for open-source projects
in great detail. This proposal suggests to follow Django's example.

There are some additional benefits for |FreeIPA|, too.

* |FreeIPA|'s `Python code style`_, :pep:`8`, and style checking with
  `pycodestyle`_ don't enforce a single code style. There is some room for
  interpretation. Developers have a slightly different interpretation how to
  indent multi-line function calls or where to put the closing brace of a
  *list*.
* A considerable amount of older code does not follow |FreeIPA|'s guidelines
  for `Python code style`_. Our linting and CI system has to work around the
  problem. For example `pycodestyle`_ checks are only applied to
  ``git diff``. When a developer has to touch old code then it is often
  necessary to first fix code style violations to make ``make fastlint``
  pass.
* Every now and then a reviewer a committer about code style and formatting
  in a pull request. This delays merge of a PR and can be source of
  frustration, especially for new contributors.
* Auto-formatting plus a handful of manual changes will get rid of all
  `pycodestyle`_ violations. As of today ``python3 -m pycodestyle`` lists
  over 25,000 style violations in |FreeIPA|.

Adoption of |black| will eliminate these issues and reduce |FreeIPA|'s
`Python code style`_ to run ``make black``.

Concerns
========

Auto-formatting |FreeIPA| with |black| will change almost all Python files of
FreIPA.

* Mass-changes of code is going to make backports harder because there will
  be a lot of merge conflicts. To reduce merge conflicts all active branches
  have to be auto-formatted with |black| at once.
* Pull requests will also be affected by merge conflicts. Therefore we should
  try to merge or close as many pull requests as possible before the code is
  formatted.
* Auto formatting is going to interfere with git history and make it harder
  to find  issues. Git has an option to ignore revisions with
  ``git blame --ignore-revs-file`` or ``blame.ignoreRevsFile`` option [1]_.
  Once the formatting changes have landed in git, IPA can provide a
  ``.gitignorerevs`` file that lists the formatting commits from all branches.
* |black| sometimes creates less readable code. It's possible to disable
  reformatting with ``# fmt: off`` / ``# fmt: on``. This document proposes a
  short list of :ref:`exceptions <black-disable-formatting>`.
* In general |black| spreads out long expression over multiple lines and
  therefore can increases lines of code. The slightly larger line length
  reduces lines of code in other cases. Reformatting increases the total
  lines of code of all ``.py`` files by about 1% (765k before, 773k after).

New code style
==============

The new code style is *whatever black does*.

The rules for i18n strings and unused variables from the |FreeIPA|
`Python code style`_ still apply, though.

|black| increases the permitted maximum line length from 79/80 characters to
88 characters. According to `black project`_'s documentation

   This number was found to produce significantly shorter files than
   sticking with 80 (the most popular), or even 79 (used by the standard
   library).

.. _black-disable-formatting:

Disable formatting
------------------

While reformatting can be disable with ``# fmt: off`` / ``# fmt: on``, this
feature should **only** be used when it arguably increases readability of
code. To paraphrase `DEP 0008`_:

   The escape hatch ``# fmt: off`` is allowed only in extreme cases where
   Black produces unreadable code, not whenever someone disagrees with the
   style choices of Black.

.. note::

   Any use of ``# fmt: off`` besides argument pairs in ``subprocess.run()`` /
   ``ipautil.run()`` should be treated as code smell and maintenance problem.

Argument pairs
~~~~~~~~~~~~~~

Argument pairs of subprocess arguments may be rearrange in such a way that
argument pairs are on the same line. The argument list must still follow
black formatting rules (double quotes, trailing commas).

.. code-block:: python

   # fmt: off
   args = [
       paths.CERTUTIL,
       "-d", dbdir,
       "-N",
       "-f", self.pwd_file,
       "-@", self.pwd_file,
   ]
   # fmt: on


Function calls with 8 or more arguments
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In case a function call

* spans more 10 or more lines (including opening and closing braces)
* all arguments are simple expressions
* and there is no simple way to refactor function call

then it's acceptable to disable auto-formatting.

.. code-block:: python

   # fmt: off
   function(
       argument_a, argument_b, argument_c, argument_d, argument_e,
       argument_f, argument_g, argument_h,
   )
   # fmt: on

.. _black-pycodestyle:

pycodestyle
===========

With |black| auto-formatting and a handful of minor patches it is finally
possible to run `pycodestyle`_ on the entire code base.

Style issues to be fixed
------------------------

* *E266* too many leading '#' for block comment
* *E302* expected 2 blank lines, found 1
* *E711* comparison to None should be 'if cond is None:'
* *E712* comparison to True should be 'if cond is True:' or 'if cond:'
* *E712* comparison to False should be 'if cond is False:' or 'if not cond:'
* *E713* test for membership should be 'not in'
* *E714* test for object identity should be 'is not'
* *E721* do not compare types, use 'isinstance()'
* *E722* do not use bare 'except'

Style issues to be ignored
--------------------------

E203 whitespace before ':'
   *E203* is not :pep:`8` conform. |black| treats slice ``:`` as binary
   operator and enforces whitespace in slices, for example ``ham[1 + 1 :]``.
E231 missing whitespace after ','
   |black| always adds a comma after all arguments, e.g. ``func(a,)``.
W503 line break before binary operator
   *W503* is not pep:`8` conform.
*W601* .has_key() is deprecated, use 'in'
   ``has_key`` is used in ``ipaldap`` and its tests. The warning can be
   re-enabled as soon we completely drop legacy APIs.
*E731* do not assign a lambda expression
   IPA creates callable from lambdas a lot. It doesn't make sense to change
   all places.
*E741* ambiguous variable name 'l'
   In several places IPA uses ``l`` as variable name. In some fonts it can
   be confused with number ``1``.

Implementation
==============

1. Create infrastructure for |black|

   * Add ``BuildRequires: black`` (|black| is available in Fedora)
   * Add ``make`` targets ``black`` and ``blacklint``
   * Create ``pyproject.toml`` to configure |black| and include Python code
     that does not have a ``.py`` file extension.
   * Exclude auto-generated plugin code in ``ipaclient/remote_plugins/2_???``
     from black. It's legacy code and no developer is going to touch the code
     any more.

2. Address remaining :ref:`pycodestyle issues <black-pycodestyle>` by either
   fixing the issue or ignoring the warning locally or globally.
   ``python3 -m pycodestyle .`` should pass without any error.
3. Backport changes from (1) and (2) to ipa-4-6 and ipa-4-6 branches.
4. Run ``make black`` in all branches and merge the changes.
5. Create ``.gitignorerevs`` file with commit hashes of |black| run from all
   active branches``.
6. Enable ``blacklint`` for ``fastlint`` and ``lint`` targets so
   local linting and linting on Azure check for black violations.
7. Update |FreeIPA|'s `Python code style`_ to mention ``make black``.


.. |black| replace:: *Black*
.. _black project: https://pypi.org/project/black/
.. _DEP 0008: https://github.com/django/deps/blob/master/accepted/0008-black.rst
.. _Python code style: https://www.freeipa.org/page/Python_Coding_Style
.. _pycodestyle: https://pycodestyle.pycqa.org
.. [1] https://git-scm.com/docs/git-blame#Documentation/git-blame.txt---ignore-revs-fileltfilegt