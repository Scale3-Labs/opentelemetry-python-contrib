# Copyright The OpenTelemetry Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from opentelemetry.trace import SpanKind, Span
from opentelemetry.trace.status import Status, StatusCode
from .span_attributes import LLMSpanAttributes
from opentelemetry.semconv._incubating.attributes import (
    gen_ai_attributes as GenAIAttributes,
    error_attributes as ErrorAttributes,
)
from .utils import (
    silently_fail,
    extract_content,
    get_llm_request_attributes,
    is_streaming,
    set_span_attribute,
    set_event_completion,
    extract_tools_prompt,
)
from opentelemetry.trace import Tracer


def chat_completions_create(tracer: Tracer):
    """Wrap the `create` method of the `ChatCompletion` class to trace it."""

    def traced_method(wrapped, instance, args, kwargs):

        llm_prompts = []

        for item in kwargs.get("messages", []):
            tools_prompt = extract_tools_prompt(item)
            llm_prompts.append(tools_prompt if tools_prompt else item)

        span_attributes = {
            **get_llm_request_attributes(kwargs, prompts=llm_prompts),
        }

        attributes = LLMSpanAttributes(**span_attributes)

        span_name = f"{attributes.gen_ai_operation_name} {attributes.gen_ai_request_model}"

        span = tracer.start_span(name=span_name, kind=SpanKind.CLIENT)
        _set_input_attributes(span, attributes)

        try:
            result = wrapped(*args, **kwargs)
            if is_streaming(kwargs):
                return StreamWrapper(
                    result,
                    span,
                    function_call=kwargs.get("functions") is not None,
                    tool_calls=kwargs.get("tools") is not None,
                )
            else:
                _set_response_attributes(span, result)
                span.end()
                return result

        except Exception as error:
            span.set_status(Status(StatusCode.ERROR, str(error)))
            span.set_attribute(
                ErrorAttributes.ERROR_TYPE, type(error).__qualname__
            )
            span.end()
            raise

    return traced_method


@silently_fail
def _set_input_attributes(span, attributes: LLMSpanAttributes):
    for field, value in attributes.model_dump(by_alias=True).items():
        set_span_attribute(span, field, value)


@silently_fail
def _set_response_attributes(span, result):
    set_span_attribute(
        span, GenAIAttributes.GEN_AI_RESPONSE_MODEL, result.model
    )
    if getattr(result, "choices", None):
        choices = result.choices
        responses = [
            {
                "role": (
                    choice.message.role
                    if choice.message and choice.message.role
                    else "assistant"
                ),
                "content": extract_content(choice),
                **(
                    {
                        "content_filter_results": choice[
                            "content_filter_results"
                        ]
                    }
                    if "content_filter_results" in choice
                    else {}
                ),
            }
            for choice in choices
        ]
        for choice in choices:
            if choice.finish_reason:
                set_span_attribute(
                    span,
                    GenAIAttributes.GEN_AI_RESPONSE_FINISH_REASONS,
                    choice.finish_reason,
                )
        set_event_completion(span, responses)

    if getattr(result, "id", None):
        set_span_attribute(span, GenAIAttributes.GEN_AI_RESPONSE_ID, result.id)

    # Get the usage
    if getattr(result, "usage", None):
        set_span_attribute(
            span,
            GenAIAttributes.GEN_AI_USAGE_INPUT_TOKENS,
            result.usage.prompt_tokens,
        )
        set_span_attribute(
            span,
            GenAIAttributes.GEN_AI_USAGE_OUTPUT_TOKENS,
            result.usage.completion_tokens,
        )


class StreamWrapper:
    span: Span
    response_id: str = ""
    response_model: str = ""

    def __init__(
        self,
        stream,
        span,
        prompt_tokens=0,
        function_call=False,
        tool_calls=False,
    ):
        self.stream = stream
        self.span = span
        self.prompt_tokens = prompt_tokens
        self.function_call = function_call
        self.tool_calls = tool_calls
        self.result_content = []
        self.completion_tokens = 0
        self._span_started = False
        self.setup()

    def setup(self):
        if not self._span_started:
            self._span_started = True

    def cleanup(self):
        if self._span_started:
            if self.response_model:
                set_span_attribute(
                    self.span,
                    GenAIAttributes.GEN_AI_RESPONSE_MODEL,
                    self.response_model,
                )

            if self.response_id:
                set_span_attribute(
                    self.span,
                    GenAIAttributes.GEN_AI_RESPONSE_ID,
                    self.response_id,
                )

            set_span_attribute(
                self.span,
                GenAIAttributes.GEN_AI_USAGE_INPUT_TOKENS,
                self.prompt_tokens,
            )
            set_span_attribute(
                self.span,
                GenAIAttributes.GEN_AI_USAGE_OUTPUT_TOKENS,
                self.completion_tokens,
            )
            set_event_completion(
                self.span,
                [
                    {
                        "role": "assistant",
                        "content": "".join(self.result_content),
                    }
                ],
            )

            self.span.end()
            self._span_started = False

    def __enter__(self):
        self.setup()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self.stream)
            self.process_chunk(chunk)
            return chunk
        except StopIteration:
            self.cleanup()
            raise

    def set_response_model(self, chunk):
        if self.response_model:
            return

        if getattr(chunk, "model", None):
            self.response_model = chunk.model

    def set_response_id(self, chunk):
        if self.response_id:
            return

        if getattr(chunk, "id", None):
            self.response_id = chunk.id

    def build_streaming_response(self, chunk):
        if getattr(chunk, "choices", None) is None:
            return

        choices = chunk.choices
        content = []
        if not self.function_call and not self.tool_calls:
            for choice in choices:
                if choice.delta and choice.delta.content is not None:
                    content = [choice.delta.content]

        elif self.function_call:
            for choice in choices:
                if (
                    choice.delta
                    and choice.delta.function_call is not None
                    and choice.delta.function_call.arguments is not None
                ):
                    content = [choice.delta.function_call.arguments]

        elif self.tool_calls:
            for choice in choices:
                if choice.delta and choice.delta.tool_calls is not None:
                    toolcalls = choice.delta.tool_calls
                    content = []
                    for tool_call in toolcalls:
                        if (
                            tool_call
                            and tool_call.function is not None
                            and tool_call.function.arguments is not None
                        ):
                            content.append(tool_call.function.arguments)

        for choice in choices:
            finish_reason = choice.finish_reason
            if finish_reason:
                set_span_attribute(
                    self.span,
                    GenAIAttributes.GEN_AI_RESPONSE_FINISH_REASONS,
                    finish_reason,
                )
        if content:
            self.result_content.append(content[0])

    def set_usage(self, chunk):
        if getattr(chunk, "usage", None):
            self.completion_tokens = chunk.usage.completion_tokens
            self.prompt_tokens = chunk.usage.prompt_tokens

    def process_chunk(self, chunk):
        self.set_response_id(chunk)
        self.set_response_model(chunk)
        self.build_streaming_response(chunk)
        self.set_usage(chunk)
