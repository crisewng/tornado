from __future__ import absolute_import, division, print_function

import gc
import contextlib
import datetime
import functools
import platform
import sys
import textwrap
import time
import weakref
import warnings

from tornado.concurrent import return_future, Future
from tornado.escape import url_escape
from tornado.httpclient import AsyncHTTPClient
from tornado.ioloop import IOLoop
from tornado.log import app_log
from tornado import stack_context
from tornado.testing import AsyncHTTPTestCase, AsyncTestCase, ExpectLog, gen_test
from tornado.test.util import unittest, skipOnTravis, skipBefore33, skipBefore35, skipNotCPython, exec_test, ignore_deprecation  # noqa: E501
from tornado.web import Application, RequestHandler, asynchronous, HTTPError

from tornado import gen

try:
    from concurrent import futures
except ImportError:
    futures = None

try:
    import asyncio
except ImportError:
    asyncio = None


class GenEngineTest(AsyncTestCase):
    def setUp(self):
        self.warning_catcher = warnings.catch_warnings()
        self.warning_catcher.__enter__()
        warnings.simplefilter('ignore', DeprecationWarning)
        super(GenEngineTest, self).setUp()
        self.named_contexts = []

    def tearDown(self):
        super(GenEngineTest, self).tearDown()
        self.warning_catcher.__exit__(None, None, None)

    def named_context(self, name):
        @contextlib.contextmanager
        def context():
            self.named_contexts.append(name)
            try:
                yield
            finally:
                self.assertEqual(self.named_contexts.pop(), name)
        return context

    def run_gen(self, f):
        f()
        return self.wait()

    def delay_callback(self, iterations, callback, arg):
        """Runs callback(arg) after a number of IOLoop iterations."""
        if iterations == 0:
            callback(arg)
        else:
            self.io_loop.add_callback(functools.partial(
                self.delay_callback, iterations - 1, callback, arg))

    @return_future
    def async_future(self, result, callback):
        self.io_loop.add_callback(callback, result)

    @gen.coroutine
    def async_exception(self, e):
        yield gen.moment
        raise e

    def test_no_yield(self):
        @gen.engine
        def f():
            self.stop()
        self.run_gen(f)

    def test_inline_cb(self):
        @gen.engine
        def f():
            (yield gen.Callback("k1"))()
            res = yield gen.Wait("k1")
            self.assertTrue(res is None)
            self.stop()
        self.run_gen(f)

    def test_ioloop_cb(self):
        @gen.engine
        def f():
            self.io_loop.add_callback((yield gen.Callback("k1")))
            yield gen.Wait("k1")
            self.stop()
        self.run_gen(f)

    def test_exception_phase1(self):
        @gen.engine
        def f():
            1 / 0
        self.assertRaises(ZeroDivisionError, self.run_gen, f)

    def test_exception_phase2(self):
        @gen.engine
        def f():
            self.io_loop.add_callback((yield gen.Callback("k1")))
            yield gen.Wait("k1")
            1 / 0
        self.assertRaises(ZeroDivisionError, self.run_gen, f)

    def test_exception_in_task_phase1(self):
        def fail_task(callback):
            1 / 0

        @gen.engine
        def f():
            try:
                yield gen.Task(fail_task)
                raise Exception("did not get expected exception")
            except ZeroDivisionError:
                self.stop()
        self.run_gen(f)

    def test_exception_in_task_phase2(self):
        # This is the case that requires the use of stack_context in gen.engine
        def fail_task(callback):
            self.io_loop.add_callback(lambda: 1 / 0)

        @gen.engine
        def f():
            try:
                yield gen.Task(fail_task)
                raise Exception("did not get expected exception")
            except ZeroDivisionError:
                self.stop()
        self.run_gen(f)

    def test_with_arg(self):
        @gen.engine
        def f():
            (yield gen.Callback("k1"))(42)
            res = yield gen.Wait("k1")
            self.assertEqual(42, res)
            self.stop()
        self.run_gen(f)

    def test_with_arg_tuple(self):
        @gen.engine
        def f():
            (yield gen.Callback((1, 2)))((3, 4))
            res = yield gen.Wait((1, 2))
            self.assertEqual((3, 4), res)
            self.stop()
        self.run_gen(f)

    def test_key_reuse(self):
        @gen.engine
        def f():
            yield gen.Callback("k1")
            yield gen.Callback("k1")
            self.stop()
        self.assertRaises(gen.KeyReuseError, self.run_gen, f)

    def test_key_reuse_tuple(self):
        @gen.engine
        def f():
            yield gen.Callback((1, 2))
            yield gen.Callback((1, 2))
            self.stop()
        self.assertRaises(gen.KeyReuseError, self.run_gen, f)

    def test_key_mismatch(self):
        @gen.engine
        def f():
            yield gen.Callback("k1")
            yield gen.Wait("k2")
            self.stop()
        self.assertRaises(gen.UnknownKeyError, self.run_gen, f)

    def test_key_mismatch_tuple(self):
        @gen.engine
        def f():
            yield gen.Callback((1, 2))
            yield gen.Wait((2, 3))
            self.stop()
        self.assertRaises(gen.UnknownKeyError, self.run_gen, f)

    def test_leaked_callback(self):
        @gen.engine
        def f():
            yield gen.Callback("k1")
            self.stop()
        self.assertRaises(gen.LeakedCallbackError, self.run_gen, f)

    def test_leaked_callback_tuple(self):
        @gen.engine
        def f():
            yield gen.Callback((1, 2))
            self.stop()
        self.assertRaises(gen.LeakedCallbackError, self.run_gen, f)

    def test_parallel_callback(self):
        @gen.engine
        def f():
            for k in range(3):
                self.io_loop.add_callback((yield gen.Callback(k)))
            yield gen.Wait(1)
            self.io_loop.add_callback((yield gen.Callback(3)))
            yield gen.Wait(0)
            yield gen.Wait(3)
            yield gen.Wait(2)
            self.stop()
        self.run_gen(f)

    def test_bogus_yield(self):
        @gen.engine
        def f():
            yield 42
        self.assertRaises(gen.BadYieldError, self.run_gen, f)

    def test_bogus_yield_tuple(self):
        @gen.engine
        def f():
            yield (1, 2)
        self.assertRaises(gen.BadYieldError, self.run_gen, f)

    def test_reuse(self):
        @gen.engine
        def f():
            self.io_loop.add_callback((yield gen.Callback(0)))
            yield gen.Wait(0)
            self.stop()
        self.run_gen(f)
        self.run_gen(f)

    def test_task(self):
        @gen.engine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            self.stop()
        self.run_gen(f)

    def test_wait_all(self):
        @gen.engine
        def f():
            (yield gen.Callback("k1"))("v1")
            (yield gen.Callback("k2"))("v2")
            results = yield gen.WaitAll(["k1", "k2"])
            self.assertEqual(results, ["v1", "v2"])
            self.stop()
        self.run_gen(f)

    def test_exception_in_yield(self):
        @gen.engine
        def f():
            try:
                yield gen.Wait("k1")
                raise Exception("did not get expected exception")
            except gen.UnknownKeyError:
                pass
            self.stop()
        self.run_gen(f)

    def test_resume_after_exception_in_yield(self):
        @gen.engine
        def f():
            try:
                yield gen.Wait("k1")
                raise Exception("did not get expected exception")
            except gen.UnknownKeyError:
                pass
            (yield gen.Callback("k2"))("v2")
            self.assertEqual((yield gen.Wait("k2")), "v2")
            self.stop()
        self.run_gen(f)

    def test_orphaned_callback(self):
        @gen.engine
        def f():
            self.orphaned_callback = yield gen.Callback(1)
        try:
            self.run_gen(f)
            raise Exception("did not get expected exception")
        except gen.LeakedCallbackError:
            pass
        self.orphaned_callback()

    def test_none(self):
        @gen.engine
        def f():
            yield None
            self.stop()
        self.run_gen(f)

    def test_multi(self):
        @gen.engine
        def f():
            (yield gen.Callback("k1"))("v1")
            (yield gen.Callback("k2"))("v2")
            results = yield [gen.Wait("k1"), gen.Wait("k2")]
            self.assertEqual(results, ["v1", "v2"])
            self.stop()
        self.run_gen(f)

    def test_multi_dict(self):
        @gen.engine
        def f():
            (yield gen.Callback("k1"))("v1")
            (yield gen.Callback("k2"))("v2")
            results = yield dict(foo=gen.Wait("k1"), bar=gen.Wait("k2"))
            self.assertEqual(results, dict(foo="v1", bar="v2"))
            self.stop()
        self.run_gen(f)

    # The following tests explicitly run with both gen.Multi
    # and gen.multi_future (Task returns a Future, so it can be used
    # with either).
    def test_multi_yieldpoint_delayed(self):
        @gen.engine
        def f():
            # callbacks run at different times
            responses = yield gen.Multi([
                gen.Task(self.delay_callback, 3, arg="v1"),
                gen.Task(self.delay_callback, 1, arg="v2"),
            ])
            self.assertEqual(responses, ["v1", "v2"])
            self.stop()
        self.run_gen(f)

    def test_multi_yieldpoint_dict_delayed(self):
        @gen.engine
        def f():
            # callbacks run at different times
            responses = yield gen.Multi(dict(
                foo=gen.Task(self.delay_callback, 3, arg="v1"),
                bar=gen.Task(self.delay_callback, 1, arg="v2"),
            ))
            self.assertEqual(responses, dict(foo="v1", bar="v2"))
            self.stop()
        self.run_gen(f)

    def test_multi_future_delayed(self):
        @gen.engine
        def f():
            # callbacks run at different times
            responses = yield gen.multi_future([
                gen.Task(self.delay_callback, 3, arg="v1"),
                gen.Task(self.delay_callback, 1, arg="v2"),
            ])
            self.assertEqual(responses, ["v1", "v2"])
            self.stop()
        self.run_gen(f)

    def test_multi_future_dict_delayed(self):
        @gen.engine
        def f():
            # callbacks run at different times
            responses = yield gen.multi_future(dict(
                foo=gen.Task(self.delay_callback, 3, arg="v1"),
                bar=gen.Task(self.delay_callback, 1, arg="v2"),
            ))
            self.assertEqual(responses, dict(foo="v1", bar="v2"))
            self.stop()
        self.run_gen(f)

    @skipOnTravis
    @gen_test
    def test_multi_performance(self):
        # Yielding a list used to have quadratic performance; make
        # sure a large list stays reasonable.  On my laptop a list of
        # 2000 used to take 1.8s, now it takes 0.12.
        start = time.time()
        yield [gen.Task(self.io_loop.add_callback) for i in range(2000)]
        end = time.time()
        self.assertLess(end - start, 1.0)

    @gen_test
    def test_multi_empty(self):
        # Empty lists or dicts should return the same type.
        x = yield []
        self.assertTrue(isinstance(x, list))
        y = yield {}
        self.assertTrue(isinstance(y, dict))

    @gen_test
    def test_multi_mixed_types(self):
        # A YieldPoint (Wait) and Future (Task) can be combined
        # (and use the YieldPoint codepath)
        (yield gen.Callback("k1"))("v1")
        responses = yield [gen.Wait("k1"),
                           gen.Task(self.delay_callback, 3, arg="v2")]
        self.assertEqual(responses, ["v1", "v2"])

    @gen_test
    def test_future(self):
        result = yield self.async_future(1)
        self.assertEqual(result, 1)

    @gen_test
    def test_multi_future(self):
        results = yield [self.async_future(1), self.async_future(2)]
        self.assertEqual(results, [1, 2])

    @gen_test
    def test_multi_future_duplicate(self):
        f = self.async_future(2)
        results = yield [self.async_future(1), f, self.async_future(3), f]
        self.assertEqual(results, [1, 2, 3, 2])

    @gen_test
    def test_multi_dict_future(self):
        results = yield dict(foo=self.async_future(1), bar=self.async_future(2))
        self.assertEqual(results, dict(foo=1, bar=2))

    @gen_test
    def test_multi_exceptions(self):
        with ExpectLog(app_log, "Multiple exceptions in yield list"):
            with self.assertRaises(RuntimeError) as cm:
                yield gen.Multi([self.async_exception(RuntimeError("error 1")),
                                 self.async_exception(RuntimeError("error 2"))])
        self.assertEqual(str(cm.exception), "error 1")

        # With only one exception, no error is logged.
        with self.assertRaises(RuntimeError):
            yield gen.Multi([self.async_exception(RuntimeError("error 1")),
                             self.async_future(2)])

        # Exception logging may be explicitly quieted.
        with self.assertRaises(RuntimeError):
            yield gen.Multi([self.async_exception(RuntimeError("error 1")),
                             self.async_exception(RuntimeError("error 2"))],
                            quiet_exceptions=RuntimeError)

    @gen_test
    def test_multi_future_exceptions(self):
        with ExpectLog(app_log, "Multiple exceptions in yield list"):
            with self.assertRaises(RuntimeError) as cm:
                yield [self.async_exception(RuntimeError("error 1")),
                       self.async_exception(RuntimeError("error 2"))]
        self.assertEqual(str(cm.exception), "error 1")

        # With only one exception, no error is logged.
        with self.assertRaises(RuntimeError):
            yield [self.async_exception(RuntimeError("error 1")),
                   self.async_future(2)]

        # Exception logging may be explicitly quieted.
        with self.assertRaises(RuntimeError):
            yield gen.multi_future(
                [self.async_exception(RuntimeError("error 1")),
                 self.async_exception(RuntimeError("error 2"))],
                quiet_exceptions=RuntimeError)

    def test_arguments(self):
        @gen.engine
        def f():
            (yield gen.Callback("noargs"))()
            self.assertEqual((yield gen.Wait("noargs")), None)
            (yield gen.Callback("1arg"))(42)
            self.assertEqual((yield gen.Wait("1arg")), 42)

            (yield gen.Callback("kwargs"))(value=42)
            result = yield gen.Wait("kwargs")
            self.assertTrue(isinstance(result, gen.Arguments))
            self.assertEqual(((), dict(value=42)), result)
            self.assertEqual(dict(value=42), result.kwargs)

            (yield gen.Callback("2args"))(42, 43)
            result = yield gen.Wait("2args")
            self.assertTrue(isinstance(result, gen.Arguments))
            self.assertEqual(((42, 43), {}), result)
            self.assertEqual((42, 43), result.args)

            def task_func(callback):
                callback(None, error="foo")
            result = yield gen.Task(task_func)
            self.assertTrue(isinstance(result, gen.Arguments))
            self.assertEqual(((None,), dict(error="foo")), result)

            self.stop()
        self.run_gen(f)

    def test_stack_context_leak(self):
        # regression test: repeated invocations of a gen-based
        # function should not result in accumulated stack_contexts
        def _stack_depth():
            head = stack_context._state.contexts[1]
            length = 0

            while head is not None:
                length += 1
                head = head.old_contexts[1]

            return length

        @gen.engine
        def inner(callback):
            yield gen.Task(self.io_loop.add_callback)
            callback()

        @gen.engine
        def outer():
            for i in range(10):
                yield gen.Task(inner)

            stack_increase = _stack_depth() - initial_stack_depth
            self.assertTrue(stack_increase <= 2)
            self.stop()
        initial_stack_depth = _stack_depth()
        self.run_gen(outer)

    def test_stack_context_leak_exception(self):
        # same as previous, but with a function that exits with an exception
        @gen.engine
        def inner(callback):
            yield gen.Task(self.io_loop.add_callback)
            1 / 0

        @gen.engine
        def outer():
            for i in range(10):
                try:
                    yield gen.Task(inner)
                except ZeroDivisionError:
                    pass
            stack_increase = len(stack_context._state.contexts) - initial_stack_depth
            self.assertTrue(stack_increase <= 2)
            self.stop()
        initial_stack_depth = len(stack_context._state.contexts)
        self.run_gen(outer)

    def function_with_stack_context(self, callback):
        # Technically this function should stack_context.wrap its callback
        # upon entry.  However, it is very common for this step to be
        # omitted.
        def step2():
            self.assertEqual(self.named_contexts, ['a'])
            self.io_loop.add_callback(callback)

        with stack_context.StackContext(self.named_context('a')):
            self.io_loop.add_callback(step2)

    @gen_test
    def test_wait_transfer_stack_context(self):
        # Wait should not pick up contexts from where callback was invoked,
        # even if that function improperly fails to wrap its callback.
        cb = yield gen.Callback('k1')
        self.function_with_stack_context(cb)
        self.assertEqual(self.named_contexts, [])
        yield gen.Wait('k1')
        self.assertEqual(self.named_contexts, [])

    @gen_test
    def test_task_transfer_stack_context(self):
        yield gen.Task(self.function_with_stack_context)
        self.assertEqual(self.named_contexts, [])

    def test_raise_after_stop(self):
        # This pattern will be used in the following tests so make sure
        # the exception propagates as expected.
        @gen.engine
        def f():
            self.stop()
            1 / 0

        with self.assertRaises(ZeroDivisionError):
            self.run_gen(f)

    def test_sync_raise_return(self):
        # gen.Return is allowed in @gen.engine, but it may not be used
        # to return a value.
        @gen.engine
        def f():
            self.stop(42)
            raise gen.Return()

        result = self.run_gen(f)
        self.assertEqual(result, 42)

    def test_async_raise_return(self):
        @gen.engine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            self.stop(42)
            raise gen.Return()

        result = self.run_gen(f)
        self.assertEqual(result, 42)

    def test_sync_raise_return_value(self):
        @gen.engine
        def f():
            raise gen.Return(42)

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    def test_sync_raise_return_value_tuple(self):
        @gen.engine
        def f():
            raise gen.Return((1, 2))

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    def test_async_raise_return_value(self):
        @gen.engine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            raise gen.Return(42)

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    def test_async_raise_return_value_tuple(self):
        @gen.engine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            raise gen.Return((1, 2))

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    def test_return_value(self):
        # It is an error to apply @gen.engine to a function that returns
        # a value.
        @gen.engine
        def f():
            return 42

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    def test_return_value_tuple(self):
        # It is an error to apply @gen.engine to a function that returns
        # a value.
        @gen.engine
        def f():
            return (1, 2)

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    @skipNotCPython
    def test_task_refcounting(self):
        # On CPython, tasks and their arguments should be released immediately
        # without waiting for garbage collection.
        @gen.engine
        def f():
            class Foo(object):
                pass
            arg = Foo()
            self.arg_ref = weakref.ref(arg)
            task = gen.Task(self.io_loop.add_callback, arg=arg)
            self.task_ref = weakref.ref(task)
            yield task
            self.stop()

        self.run_gen(f)
        self.assertIs(self.arg_ref(), None)
        self.assertIs(self.task_ref(), None)


# GenBasicTest duplicates the non-deprecated portions of GenEngineTest
# with gen.coroutine to ensure we don't lose coverage when gen.engine
# goes away.
class GenBasicTest(AsyncTestCase):
    @gen.coroutine
    def delay(self, iterations, arg):
        """Returns arg after a number of IOLoop iterations."""
        for i in range(iterations):
            yield gen.moment
        raise gen.Return(arg)

    @return_future
    def async_future(self, result, callback):
        self.io_loop.add_callback(callback, result)

    @gen.coroutine
    def async_exception(self, e):
        yield gen.moment
        raise e

    @gen.coroutine
    def add_one_async(self, x):
        yield gen.moment
        raise gen.Return(x + 1)

    def test_no_yield(self):
        @gen.coroutine
        def f():
            pass
        self.io_loop.run_sync(f)

    def test_exception_phase1(self):
        @gen.coroutine
        def f():
            1 / 0
        self.assertRaises(ZeroDivisionError, self.io_loop.run_sync, f)

    def test_exception_phase2(self):
        @gen.coroutine
        def f():
            yield gen.moment
            1 / 0
        self.assertRaises(ZeroDivisionError, self.io_loop.run_sync, f)

    def test_bogus_yield(self):
        @gen.coroutine
        def f():
            yield 42
        self.assertRaises(gen.BadYieldError, self.io_loop.run_sync, f)

    def test_bogus_yield_tuple(self):
        @gen.coroutine
        def f():
            yield (1, 2)
        self.assertRaises(gen.BadYieldError, self.io_loop.run_sync, f)

    def test_reuse(self):
        @gen.coroutine
        def f():
            yield gen.moment
        self.io_loop.run_sync(f)
        self.io_loop.run_sync(f)

    def test_none(self):
        @gen.coroutine
        def f():
            yield None
        self.io_loop.run_sync(f)

    def test_multi(self):
        @gen.coroutine
        def f():
            results = yield [self.add_one_async(1), self.add_one_async(2)]
            self.assertEqual(results, [2, 3])
        self.io_loop.run_sync(f)

    def test_multi_dict(self):
        @gen.coroutine
        def f():
            results = yield dict(foo=self.add_one_async(1), bar=self.add_one_async(2))
            self.assertEqual(results, dict(foo=2, bar=3))
        self.io_loop.run_sync(f)

    def test_multi_delayed(self):
        @gen.coroutine
        def f():
            # callbacks run at different times
            responses = yield gen.multi_future([
                self.delay(3, "v1"),
                self.delay(1, "v2"),
            ])
            self.assertEqual(responses, ["v1", "v2"])
        self.io_loop.run_sync(f)

    def test_multi_dict_delayed(self):
        @gen.coroutine
        def f():
            # callbacks run at different times
            responses = yield gen.multi_future(dict(
                foo=self.delay(3, "v1"),
                bar=self.delay(1, "v2"),
            ))
            self.assertEqual(responses, dict(foo="v1", bar="v2"))
        self.io_loop.run_sync(f)

    @skipOnTravis
    @gen_test
    def test_multi_performance(self):
        # Yielding a list used to have quadratic performance; make
        # sure a large list stays reasonable.  On my laptop a list of
        # 2000 used to take 1.8s, now it takes 0.12.
        start = time.time()
        yield [gen.moment for i in range(2000)]
        end = time.time()
        self.assertLess(end - start, 1.0)

    @gen_test
    def test_multi_empty(self):
        # Empty lists or dicts should return the same type.
        x = yield []
        self.assertTrue(isinstance(x, list))
        y = yield {}
        self.assertTrue(isinstance(y, dict))

    @gen_test
    def test_future(self):
        result = yield self.async_future(1)
        self.assertEqual(result, 1)

    @gen_test
    def test_multi_future(self):
        results = yield [self.async_future(1), self.async_future(2)]
        self.assertEqual(results, [1, 2])

    @gen_test
    def test_multi_future_duplicate(self):
        f = self.async_future(2)
        results = yield [self.async_future(1), f, self.async_future(3), f]
        self.assertEqual(results, [1, 2, 3, 2])

    @gen_test
    def test_multi_dict_future(self):
        results = yield dict(foo=self.async_future(1), bar=self.async_future(2))
        self.assertEqual(results, dict(foo=1, bar=2))

    @gen_test
    def test_multi_exceptions(self):
        with ExpectLog(app_log, "Multiple exceptions in yield list"):
            with self.assertRaises(RuntimeError) as cm:
                yield gen.Multi([self.async_exception(RuntimeError("error 1")),
                                 self.async_exception(RuntimeError("error 2"))])
        self.assertEqual(str(cm.exception), "error 1")

        # With only one exception, no error is logged.
        with self.assertRaises(RuntimeError):
            yield gen.Multi([self.async_exception(RuntimeError("error 1")),
                             self.async_future(2)])

        # Exception logging may be explicitly quieted.
        with self.assertRaises(RuntimeError):
            yield gen.Multi([self.async_exception(RuntimeError("error 1")),
                             self.async_exception(RuntimeError("error 2"))],
                            quiet_exceptions=RuntimeError)

    @gen_test
    def test_multi_future_exceptions(self):
        with ExpectLog(app_log, "Multiple exceptions in yield list"):
            with self.assertRaises(RuntimeError) as cm:
                yield [self.async_exception(RuntimeError("error 1")),
                       self.async_exception(RuntimeError("error 2"))]
        self.assertEqual(str(cm.exception), "error 1")

        # With only one exception, no error is logged.
        with self.assertRaises(RuntimeError):
            yield [self.async_exception(RuntimeError("error 1")),
                   self.async_future(2)]

        # Exception logging may be explicitly quieted.
        with self.assertRaises(RuntimeError):
            yield gen.multi_future(
                [self.async_exception(RuntimeError("error 1")),
                 self.async_exception(RuntimeError("error 2"))],
                quiet_exceptions=RuntimeError)

    def test_sync_raise_return(self):
        @gen.coroutine
        def f():
            raise gen.Return()

        self.io_loop.run_sync(f)

    def test_async_raise_return(self):
        @gen.coroutine
        def f():
            yield gen.moment
            raise gen.Return()

        self.io_loop.run_sync(f)

    def test_sync_raise_return_value(self):
        @gen.coroutine
        def f():
            raise gen.Return(42)

        self.assertEqual(42, self.io_loop.run_sync(f))

    def test_sync_raise_return_value_tuple(self):
        @gen.coroutine
        def f():
            raise gen.Return((1, 2))

        self.assertEqual((1, 2), self.io_loop.run_sync(f))

    def test_async_raise_return_value(self):
        @gen.coroutine
        def f():
            yield gen.moment
            raise gen.Return(42)

        self.assertEqual(42, self.io_loop.run_sync(f))

    def test_async_raise_return_value_tuple(self):
        @gen.coroutine
        def f():
            yield gen.moment
            raise gen.Return((1, 2))

        self.assertEqual((1, 2), self.io_loop.run_sync(f))


class GenCoroutineTest(AsyncTestCase):
    def setUp(self):
        # Stray StopIteration exceptions can lead to tests exiting prematurely,
        # so we need explicit checks here to make sure the tests run all
        # the way through.
        self.finished = False
        super(GenCoroutineTest, self).setUp()

    def tearDown(self):
        super(GenCoroutineTest, self).tearDown()
        assert self.finished

    def test_attributes(self):
        self.finished = True

        def f():
            yield gen.moment

        coro = gen.coroutine(f)
        self.assertEqual(coro.__name__, f.__name__)
        self.assertEqual(coro.__module__, f.__module__)
        self.assertIs(coro.__wrapped__, f)

    def test_is_coroutine_function(self):
        self.finished = True

        def f():
            yield gen.moment

        coro = gen.coroutine(f)
        self.assertFalse(gen.is_coroutine_function(f))
        self.assertTrue(gen.is_coroutine_function(coro))
        self.assertFalse(gen.is_coroutine_function(coro()))

    @gen_test
    def test_sync_gen_return(self):
        @gen.coroutine
        def f():
            raise gen.Return(42)
        result = yield f()
        self.assertEqual(result, 42)
        self.finished = True

    @gen_test
    def test_async_gen_return(self):
        @gen.coroutine
        def f():
            yield gen.moment
            raise gen.Return(42)
        result = yield f()
        self.assertEqual(result, 42)
        self.finished = True

    @gen_test
    def test_sync_return(self):
        @gen.coroutine
        def f():
            return 42
        result = yield f()
        self.assertEqual(result, 42)
        self.finished = True

    @skipBefore33
    @gen_test
    def test_async_return(self):
        namespace = exec_test(globals(), locals(), """
        @gen.coroutine
        def f():
            yield gen.moment
            return 42
        """)
        result = yield namespace['f']()
        self.assertEqual(result, 42)
        self.finished = True

    @skipBefore33
    @gen_test
    def test_async_early_return(self):
        # A yield statement exists but is not executed, which means
        # this function "returns" via an exception.  This exception
        # doesn't happen before the exception handling is set up.
        namespace = exec_test(globals(), locals(), """
        @gen.coroutine
        def f():
            if True:
                return 42
            yield gen.Task(self.io_loop.add_callback)
        """)
        result = yield namespace['f']()
        self.assertEqual(result, 42)
        self.finished = True

    @skipBefore35
    @gen_test
    def test_async_await(self):
        @gen.coroutine
        def f1():
            yield gen.moment
            raise gen.Return(42)

        # This test verifies that an async function can await a
        # yield-based gen.coroutine, and that a gen.coroutine
        # (the test method itself) can yield an async function.
        namespace = exec_test(globals(), locals(), """
        async def f2():
            result = await f1()
            return result
        """)
        result = yield namespace['f2']()
        self.assertEqual(result, 42)
        self.finished = True

    @skipBefore35
    @gen_test
    def test_asyncio_sleep_zero(self):
        # asyncio.sleep(0) turns into a special case (equivalent to
        # `yield None`)
        namespace = exec_test(globals(), locals(), """
        async def f():
            import asyncio
            await asyncio.sleep(0)
            return 42
        """)
        result = yield namespace['f']()
        self.assertEqual(result, 42)
        self.finished = True

    @skipBefore35
    @gen_test
    def test_async_await_mixed_multi_native_future(self):
        @gen.coroutine
        def f1():
            yield gen.moment

        namespace = exec_test(globals(), locals(), """
        async def f2():
            await f1()
            return 42
        """)

        @gen.coroutine
        def f3():
            yield gen.moment
            raise gen.Return(43)

        results = yield [namespace['f2'](), f3()]
        self.assertEqual(results, [42, 43])
        self.finished = True

    @skipBefore35
    @gen_test
    def test_async_await_mixed_multi_native_yieldpoint(self):
        namespace = exec_test(globals(), locals(), """
        async def f1():
            await gen.Task(self.io_loop.add_callback)
            return 42
        """)

        @gen.coroutine
        def f2():
            yield gen.Task(self.io_loop.add_callback)
            raise gen.Return(43)

        with ignore_deprecation():
            f2(callback=(yield gen.Callback('cb')))
            results = yield [namespace['f1'](), gen.Wait('cb')]
        self.assertEqual(results, [42, 43])
        self.finished = True

    @skipBefore35
    @gen_test
    def test_async_with_timeout(self):
        namespace = exec_test(globals(), locals(), """
        async def f1():
            return 42
        """)

        result = yield gen.with_timeout(datetime.timedelta(hours=1),
                                        namespace['f1']())
        self.assertEqual(result, 42)
        self.finished = True

    @gen_test
    def test_sync_return_no_value(self):
        @gen.coroutine
        def f():
            return
        result = yield f()
        self.assertEqual(result, None)
        self.finished = True

    @gen_test
    def test_async_return_no_value(self):
        # Without a return value we don't need python 3.3.
        @gen.coroutine
        def f():
            yield gen.moment
            return
        result = yield f()
        self.assertEqual(result, None)
        self.finished = True

    @gen_test
    def test_sync_raise(self):
        @gen.coroutine
        def f():
            1 / 0
        # The exception is raised when the future is yielded
        # (or equivalently when its result method is called),
        # not when the function itself is called).
        future = f()
        with self.assertRaises(ZeroDivisionError):
            yield future
        self.finished = True

    @gen_test
    def test_async_raise(self):
        @gen.coroutine
        def f():
            yield gen.moment
            1 / 0
        future = f()
        with self.assertRaises(ZeroDivisionError):
            yield future
        self.finished = True

    @gen_test
    def test_pass_callback(self):
        with ignore_deprecation():
            @gen.coroutine
            def f():
                raise gen.Return(42)
            result = yield gen.Task(f)
        self.assertEqual(result, 42)
        self.finished = True

    @gen_test
    def test_replace_yieldpoint_exception(self):
        # Test exception handling: a coroutine can catch one exception
        # raised by a yield point and raise a different one.
        @gen.coroutine
        def f1():
            1 / 0

        @gen.coroutine
        def f2():
            try:
                yield f1()
            except ZeroDivisionError:
                raise KeyError()

        future = f2()
        with self.assertRaises(KeyError):
            yield future
        self.finished = True

    @gen_test
    def test_swallow_yieldpoint_exception(self):
        # Test exception handling: a coroutine can catch an exception
        # raised by a yield point and not raise a different one.
        @gen.coroutine
        def f1():
            1 / 0

        @gen.coroutine
        def f2():
            try:
                yield f1()
            except ZeroDivisionError:
                raise gen.Return(42)

        result = yield f2()
        self.assertEqual(result, 42)
        self.finished = True

    @gen_test
    def test_replace_context_exception(self):
        with ignore_deprecation():
            # Test exception handling: exceptions thrown into the stack context
            # can be caught and replaced.
            # Note that this test and the following are for behavior that is
            # not really supported any more:  coroutines no longer create a
            # stack context automatically; but one is created after the first
            # YieldPoint (i.e. not a Future).
            @gen.coroutine
            def f2():
                (yield gen.Callback(1))()
                yield gen.Wait(1)
                self.io_loop.add_callback(lambda: 1 / 0)
                try:
                    yield gen.Task(self.io_loop.add_timeout,
                                   self.io_loop.time() + 10)
                except ZeroDivisionError:
                    raise KeyError()

            future = f2()
            with self.assertRaises(KeyError):
                yield future
            self.finished = True

    @gen_test
    def test_swallow_context_exception(self):
        with ignore_deprecation():
            # Test exception handling: exceptions thrown into the stack context
            # can be caught and ignored.
            @gen.coroutine
            def f2():
                (yield gen.Callback(1))()
                yield gen.Wait(1)
                self.io_loop.add_callback(lambda: 1 / 0)
                try:
                    yield gen.Task(self.io_loop.add_timeout,
                                   self.io_loop.time() + 10)
                except ZeroDivisionError:
                    raise gen.Return(42)

            result = yield f2()
            self.assertEqual(result, 42)
            self.finished = True

    @gen_test
    def test_moment(self):
        calls = []

        @gen.coroutine
        def f(name, yieldable):
            for i in range(5):
                calls.append(name)
                yield yieldable
        # First, confirm the behavior without moment: each coroutine
        # monopolizes the event loop until it finishes.
        immediate = Future()
        immediate.set_result(None)
        yield [f('a', immediate), f('b', immediate)]
        self.assertEqual(''.join(calls), 'aaaaabbbbb')

        # With moment, they take turns.
        calls = []
        yield [f('a', gen.moment), f('b', gen.moment)]
        self.assertEqual(''.join(calls), 'ababababab')
        self.finished = True

        calls = []
        yield [f('a', gen.moment), f('b', immediate)]
        self.assertEqual(''.join(calls), 'abbbbbaaaa')

    @gen_test
    def test_sleep(self):
        yield gen.sleep(0.01)
        self.finished = True

    @skipBefore33
    @gen_test
    def test_py3_leak_exception_context(self):
        class LeakedException(Exception):
            pass

        @gen.coroutine
        def inner(iteration):
            raise LeakedException(iteration)

        try:
            yield inner(1)
        except LeakedException as e:
            self.assertEqual(str(e), "1")
            self.assertIsNone(e.__context__)

        try:
            yield inner(2)
        except LeakedException as e:
            self.assertEqual(str(e), "2")
            self.assertIsNone(e.__context__)

        self.finished = True

    @skipNotCPython
    @unittest.skipIf((3,) < sys.version_info < (3, 6),
                     "asyncio.Future has reference cycles")
    def test_coroutine_refcounting(self):
        # On CPython, tasks and their arguments should be released immediately
        # without waiting for garbage collection.
        @gen.coroutine
        def inner():
            class Foo(object):
                pass
            local_var = Foo()
            self.local_ref = weakref.ref(local_var)
            yield gen.coroutine(lambda: None)()
            raise ValueError('Some error')

        @gen.coroutine
        def inner2():
            try:
                yield inner()
            except ValueError:
                pass

        self.io_loop.run_sync(inner2, timeout=3)

        self.assertIs(self.local_ref(), None)
        self.finished = True

    @unittest.skipIf(sys.version_info < (3,),
                     "test only relevant with asyncio Futures")
    def test_asyncio_future_debug_info(self):
        self.finished = True
        # Enable debug mode
        asyncio_loop = asyncio.get_event_loop()
        self.addCleanup(asyncio_loop.set_debug, asyncio_loop.get_debug())
        asyncio_loop.set_debug(True)

        def f():
            yield gen.moment

        coro = gen.coroutine(f)()
        self.assertIsInstance(coro, asyncio.Future)
        # We expect the coroutine repr() to show the place where
        # it was instantiated
        expected = ("created at %s:%d"
                    % (__file__, f.__code__.co_firstlineno + 3))
        actual = repr(coro)
        self.assertIn(expected, actual)

    @unittest.skipIf(asyncio is None, "asyncio module not present")
    @gen_test
    def test_asyncio_gather(self):
        # This demonstrates that tornado coroutines can be understood
        # by asyncio (This failed prior to Tornado 5.0).
        @gen.coroutine
        def f():
            yield gen.moment
            raise gen.Return(1)

        ret = yield asyncio.gather(f(), f())
        self.assertEqual(ret, [1, 1])
        self.finished = True


class GenSequenceHandler(RequestHandler):
    with ignore_deprecation():
        @asynchronous
        @gen.engine
        def get(self):
            # The outer ignore_deprecation applies at definition time.
            # We need another for serving time.
            with ignore_deprecation():
                self.io_loop = self.request.connection.stream.io_loop
                self.io_loop.add_callback((yield gen.Callback("k1")))
                yield gen.Wait("k1")
                self.write("1")
                self.io_loop.add_callback((yield gen.Callback("k2")))
                yield gen.Wait("k2")
                self.write("2")
                # reuse an old key
                self.io_loop.add_callback((yield gen.Callback("k1")))
                yield gen.Wait("k1")
                self.finish("3")


class GenCoroutineSequenceHandler(RequestHandler):
    @gen.coroutine
    def get(self):
        yield gen.moment
        self.write("1")
        yield gen.moment
        self.write("2")
        yield gen.moment
        self.finish("3")


class GenCoroutineUnfinishedSequenceHandler(RequestHandler):
    @asynchronous
    @gen.coroutine
    def get(self):
        yield gen.moment
        self.write("1")
        yield gen.moment
        self.write("2")
        yield gen.moment
        # just write, don't finish
        self.write("3")


class GenTaskHandler(RequestHandler):
    @gen.coroutine
    def get(self):
        client = AsyncHTTPClient()
        with ignore_deprecation():
            response = yield gen.Task(client.fetch, self.get_argument('url'))
        response.rethrow()
        self.finish(b"got response: " + response.body)


class GenExceptionHandler(RequestHandler):
    with ignore_deprecation():
        @asynchronous
        @gen.engine
        def get(self):
            # This test depends on the order of the two decorators.
            io_loop = self.request.connection.stream.io_loop
            yield gen.Task(io_loop.add_callback)
            raise Exception("oops")


class GenCoroutineExceptionHandler(RequestHandler):
    @gen.coroutine
    def get(self):
        # This test depends on the order of the two decorators.
        io_loop = self.request.connection.stream.io_loop
        yield gen.Task(io_loop.add_callback)
        raise Exception("oops")


class GenYieldExceptionHandler(RequestHandler):
    @gen.coroutine
    def get(self):
        io_loop = self.request.connection.stream.io_loop
        # Test the interaction of the two stack_contexts.
        with ignore_deprecation():
            def fail_task(callback):
                io_loop.add_callback(lambda: 1 / 0)
            try:
                yield gen.Task(fail_task)
                raise Exception("did not get expected exception")
            except ZeroDivisionError:
                self.finish('ok')


# "Undecorated" here refers to the absence of @asynchronous.
class UndecoratedCoroutinesHandler(RequestHandler):
    @gen.coroutine
    def prepare(self):
        self.chunks = []
        yield gen.moment
        self.chunks.append('1')

    @gen.coroutine
    def get(self):
        self.chunks.append('2')
        yield gen.moment
        self.chunks.append('3')
        yield gen.moment
        self.write(''.join(self.chunks))


class AsyncPrepareErrorHandler(RequestHandler):
    @gen.coroutine
    def prepare(self):
        yield gen.moment
        raise HTTPError(403)

    def get(self):
        self.finish('ok')


class NativeCoroutineHandler(RequestHandler):
    if sys.version_info > (3, 5):
        exec(textwrap.dedent("""
        async def get(self):
            import asyncio
            await asyncio.sleep(0)
            self.write("ok")
        """))


class GenWebTest(AsyncHTTPTestCase):
    def get_app(self):
        return Application([
            ('/sequence', GenSequenceHandler),
            ('/coroutine_sequence', GenCoroutineSequenceHandler),
            ('/coroutine_unfinished_sequence',
             GenCoroutineUnfinishedSequenceHandler),
            ('/task', GenTaskHandler),
            ('/exception', GenExceptionHandler),
            ('/coroutine_exception', GenCoroutineExceptionHandler),
            ('/yield_exception', GenYieldExceptionHandler),
            ('/undecorated_coroutine', UndecoratedCoroutinesHandler),
            ('/async_prepare_error', AsyncPrepareErrorHandler),
            ('/native_coroutine', NativeCoroutineHandler),
        ])

    def test_sequence_handler(self):
        response = self.fetch('/sequence')
        self.assertEqual(response.body, b"123")

    def test_coroutine_sequence_handler(self):
        response = self.fetch('/coroutine_sequence')
        self.assertEqual(response.body, b"123")

    def test_coroutine_unfinished_sequence_handler(self):
        response = self.fetch('/coroutine_unfinished_sequence')
        self.assertEqual(response.body, b"123")

    def test_task_handler(self):
        response = self.fetch('/task?url=%s' % url_escape(self.get_url('/sequence')))
        self.assertEqual(response.body, b"got response: 123")

    def test_exception_handler(self):
        # Make sure we get an error and not a timeout
        with ExpectLog(app_log, "Uncaught exception GET /exception"):
            response = self.fetch('/exception')
        self.assertEqual(500, response.code)

    def test_coroutine_exception_handler(self):
        # Make sure we get an error and not a timeout
        with ExpectLog(app_log, "Uncaught exception GET /coroutine_exception"):
            response = self.fetch('/coroutine_exception')
        self.assertEqual(500, response.code)

    def test_yield_exception_handler(self):
        response = self.fetch('/yield_exception')
        self.assertEqual(response.body, b'ok')

    def test_undecorated_coroutines(self):
        response = self.fetch('/undecorated_coroutine')
        self.assertEqual(response.body, b'123')

    def test_async_prepare_error_handler(self):
        response = self.fetch('/async_prepare_error')
        self.assertEqual(response.code, 403)

    @skipBefore35
    def test_native_coroutine_handler(self):
        response = self.fetch('/native_coroutine')
        self.assertEqual(response.code, 200)
        self.assertEqual(response.body, b'ok')


class WithTimeoutTest(AsyncTestCase):
    @gen_test
    def test_timeout(self):
        with self.assertRaises(gen.TimeoutError):
            yield gen.with_timeout(datetime.timedelta(seconds=0.1),
                                   Future())

    @gen_test
    def test_completes_before_timeout(self):
        future = Future()
        self.io_loop.add_timeout(datetime.timedelta(seconds=0.1),
                                 lambda: future.set_result('asdf'))
        result = yield gen.with_timeout(datetime.timedelta(seconds=3600),
                                        future)
        self.assertEqual(result, 'asdf')

    @gen_test
    def test_fails_before_timeout(self):
        future = Future()
        self.io_loop.add_timeout(
            datetime.timedelta(seconds=0.1),
            lambda: future.set_exception(ZeroDivisionError()))
        with self.assertRaises(ZeroDivisionError):
            yield gen.with_timeout(datetime.timedelta(seconds=3600),
                                   future)

    @gen_test
    def test_already_resolved(self):
        future = Future()
        future.set_result('asdf')
        result = yield gen.with_timeout(datetime.timedelta(seconds=3600),
                                        future)
        self.assertEqual(result, 'asdf')

    @unittest.skipIf(futures is None, 'futures module not present')
    @gen_test
    def test_timeout_concurrent_future(self):
        # A concurrent future that does not resolve before the timeout.
        with futures.ThreadPoolExecutor(1) as executor:
            with self.assertRaises(gen.TimeoutError):
                yield gen.with_timeout(self.io_loop.time(),
                                       executor.submit(time.sleep, 0.1))

    @unittest.skipIf(futures is None, 'futures module not present')
    @gen_test
    def test_completed_concurrent_future(self):
        # A concurrent future that is resolved before we even submit it
        # to with_timeout.
        with futures.ThreadPoolExecutor(1) as executor:
            f = executor.submit(lambda: None)
            f.result()  # wait for completion
            yield gen.with_timeout(datetime.timedelta(seconds=3600), f)

    @unittest.skipIf(futures is None, 'futures module not present')
    @gen_test
    def test_normal_concurrent_future(self):
        # A conccurrent future that resolves while waiting for the timeout.
        with futures.ThreadPoolExecutor(1) as executor:
            yield gen.with_timeout(datetime.timedelta(seconds=3600),
                                   executor.submit(lambda: time.sleep(0.01)))


class WaitIteratorTest(AsyncTestCase):
    @gen_test
    def test_empty_iterator(self):
        g = gen.WaitIterator()
        self.assertTrue(g.done(), 'empty generator iterated')

        with self.assertRaises(ValueError):
            g = gen.WaitIterator(False, bar=False)

        self.assertEqual(g.current_index, None, "bad nil current index")
        self.assertEqual(g.current_future, None, "bad nil current future")

    @gen_test
    def test_already_done(self):
        f1 = Future()
        f2 = Future()
        f3 = Future()
        f1.set_result(24)
        f2.set_result(42)
        f3.set_result(84)

        g = gen.WaitIterator(f1, f2, f3)
        i = 0
        while not g.done():
            r = yield g.next()
            # Order is not guaranteed, but the current implementation
            # preserves ordering of already-done Futures.
            if i == 0:
                self.assertEqual(g.current_index, 0)
                self.assertIs(g.current_future, f1)
                self.assertEqual(r, 24)
            elif i == 1:
                self.assertEqual(g.current_index, 1)
                self.assertIs(g.current_future, f2)
                self.assertEqual(r, 42)
            elif i == 2:
                self.assertEqual(g.current_index, 2)
                self.assertIs(g.current_future, f3)
                self.assertEqual(r, 84)
            i += 1

        self.assertEqual(g.current_index, None, "bad nil current index")
        self.assertEqual(g.current_future, None, "bad nil current future")

        dg = gen.WaitIterator(f1=f1, f2=f2)

        while not dg.done():
            dr = yield dg.next()
            if dg.current_index == "f1":
                self.assertTrue(dg.current_future == f1 and dr == 24,
                                "WaitIterator dict status incorrect")
            elif dg.current_index == "f2":
                self.assertTrue(dg.current_future == f2 and dr == 42,
                                "WaitIterator dict status incorrect")
            else:
                self.fail("got bad WaitIterator index {}".format(
                    dg.current_index))

            i += 1

        self.assertEqual(dg.current_index, None, "bad nil current index")
        self.assertEqual(dg.current_future, None, "bad nil current future")

    def finish_coroutines(self, iteration, futures):
        if iteration == 3:
            futures[2].set_result(24)
        elif iteration == 5:
            futures[0].set_exception(ZeroDivisionError())
        elif iteration == 8:
            futures[1].set_result(42)
            futures[3].set_result(84)

        if iteration < 8:
            self.io_loop.add_callback(self.finish_coroutines, iteration + 1, futures)

    @gen_test
    def test_iterator(self):
        futures = [Future(), Future(), Future(), Future()]

        self.finish_coroutines(0, futures)

        g = gen.WaitIterator(*futures)

        i = 0
        while not g.done():
            try:
                r = yield g.next()
            except ZeroDivisionError:
                self.assertIs(g.current_future, futures[0],
                              'exception future invalid')
            else:
                if i == 0:
                    self.assertEqual(r, 24, 'iterator value incorrect')
                    self.assertEqual(g.current_index, 2, 'wrong index')
                elif i == 2:
                    self.assertEqual(r, 42, 'iterator value incorrect')
                    self.assertEqual(g.current_index, 1, 'wrong index')
                elif i == 3:
                    self.assertEqual(r, 84, 'iterator value incorrect')
                    self.assertEqual(g.current_index, 3, 'wrong index')
            i += 1

    @skipBefore35
    @gen_test
    def test_iterator_async_await(self):
        # Recreate the previous test with py35 syntax. It's a little clunky
        # because of the way the previous test handles an exception on
        # a single iteration.
        futures = [Future(), Future(), Future(), Future()]
        self.finish_coroutines(0, futures)
        self.finished = False

        namespace = exec_test(globals(), locals(), """
        async def f():
            i = 0
            g = gen.WaitIterator(*futures)
            try:
                async for r in g:
                    if i == 0:
                        self.assertEqual(r, 24, 'iterator value incorrect')
                        self.assertEqual(g.current_index, 2, 'wrong index')
                    else:
                        raise Exception("expected exception on iteration 1")
                    i += 1
            except ZeroDivisionError:
                i += 1
            async for r in g:
                if i == 2:
                    self.assertEqual(r, 42, 'iterator value incorrect')
                    self.assertEqual(g.current_index, 1, 'wrong index')
                elif i == 3:
                    self.assertEqual(r, 84, 'iterator value incorrect')
                    self.assertEqual(g.current_index, 3, 'wrong index')
                else:
                    raise Exception("didn't expect iteration %d" % i)
                i += 1
            self.finished = True
        """)
        yield namespace['f']()
        self.assertTrue(self.finished)

    @gen_test
    def test_no_ref(self):
        # In this usage, there is no direct hard reference to the
        # WaitIterator itself, only the Future it returns. Since
        # WaitIterator uses weak references internally to improve GC
        # performance, this used to cause problems.
        yield gen.with_timeout(datetime.timedelta(seconds=0.1),
                               gen.WaitIterator(gen.sleep(0)).next())


class RunnerGCTest(AsyncTestCase):
    def is_pypy3(self):
        return (platform.python_implementation() == 'PyPy' and
                sys.version_info > (3,))

    @gen_test
    def test_gc(self):
        # Github issue 1769: Runner objects can get GCed unexpectedly
        # while their future is alive.
        weakref_scope = [None]

        def callback():
            gc.collect(2)
            weakref_scope[0]().set_result(123)

        @gen.coroutine
        def tester():
            fut = Future()
            weakref_scope[0] = weakref.ref(fut)
            self.io_loop.add_callback(callback)
            yield fut

        yield gen.with_timeout(
            datetime.timedelta(seconds=0.2),
            tester()
        )

    def test_gc_infinite_coro(self):
        # Github issue 2229: suspended coroutines should be GCed when
        # their loop is closed, even if they're involved in a reference
        # cycle.
        if IOLoop.configured_class().__name__.endswith('TwistedIOLoop'):
            raise unittest.SkipTest("Test may fail on TwistedIOLoop")

        loop = self.get_new_ioloop()
        result = []
        wfut = []

        @gen.coroutine
        def infinite_coro():
            try:
                while True:
                    yield gen.sleep(1e-3)
                    result.append(True)
            finally:
                # coroutine finalizer
                result.append(None)

        @gen.coroutine
        def do_something():
            fut = infinite_coro()
            fut._refcycle = fut
            wfut.append(weakref.ref(fut))
            yield gen.sleep(0.2)

        loop.run_sync(do_something)
        loop.close()
        gc.collect()
        # Future was collected
        self.assertIs(wfut[0](), None)
        # At least one wakeup
        self.assertGreaterEqual(len(result), 2)
        if not self.is_pypy3():
            # coroutine finalizer was called (not on PyPy3 apparently)
            self.assertIs(result[-1], None)

    @skipBefore35
    def test_gc_infinite_async_await(self):
        # Same as test_gc_infinite_coro, but with a `async def` function
        import asyncio

        namespace = exec_test(globals(), locals(), """
        async def infinite_coro(result):
            try:
                while True:
                    await gen.sleep(1e-3)
                    result.append(True)
            finally:
                # coroutine finalizer
                result.append(None)
        """)

        infinite_coro = namespace['infinite_coro']
        loop = self.get_new_ioloop()
        result = []
        wfut = []

        @gen.coroutine
        def do_something():
            fut = asyncio.get_event_loop().create_task(infinite_coro(result))
            fut._refcycle = fut
            wfut.append(weakref.ref(fut))
            yield gen.sleep(0.2)

        loop.run_sync(do_something)
        with ExpectLog('asyncio', "Task was destroyed but it is pending"):
            loop.close()
            gc.collect()
        # Future was collected
        self.assertIs(wfut[0](), None)
        # At least one wakeup and one finally
        self.assertGreaterEqual(len(result), 2)
        if not self.is_pypy3():
            # coroutine finalizer was called (not on PyPy3 apparently)
            self.assertIs(result[-1], None)

    def test_multi_moment(self):
        # Test gen.multi with moment
        # now that it's not a real Future
        @gen.coroutine
        def wait_a_moment():
            result = yield gen.multi([gen.moment, gen.moment])
            raise gen.Return(result)

        loop = self.get_new_ioloop()
        result = loop.run_sync(wait_a_moment)
        self.assertEqual(result, [None, None])


if __name__ == '__main__':
    unittest.main()
