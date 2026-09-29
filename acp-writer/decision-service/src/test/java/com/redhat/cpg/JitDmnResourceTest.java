package com.redhat.cpg;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.Map;

import jakarta.ws.rs.core.Response;

import org.junit.jupiter.api.Test;

class JitDmnResourceTest {

    private final JitDmnResource resource = new JitDmnResource();

    @Test
    void validModelPassesEngineValidation() {
        Response response = resource.validate(request(validModel()));

        assertEquals(200, response.getStatus());
        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals(true, body.get("valid"));
    }

    @Test
    void feelSyntaxFailureIsReportedAsInvalid() {
        Response response = resource.validate(request(validModel().replace(">= 18", ">=")));

        assertEquals(200, response.getStatus());
        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals(false, body.get("valid"));
        assertNotNull(body.get("messages"));
        assertFalse(((java.util.List<?>) body.get("messages")).isEmpty());
        assertTrue(((java.util.List<?>) body.get("messages")).stream()
            .map(Object::toString)
            .anyMatch(message -> message.contains("FEEL") || message.contains(">=")));
    }

    @Test
    void tableGapIsReportedAsWarningNotError() {
        Response response = resource.validate(request(validModel()));

        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals(true, body.get("valid"));
        assertTrue(((java.util.List<?>) body.get("messages")).stream()
            .map(Object::toString)
            .anyMatch(message -> message.contains("WARN")
                && message.toLowerCase().contains("gap")));
    }

    @Test
    void unresolvedNameIsReportedAsInvalid() {
        Response response = resource.validate(request(validModel().replace(
            "<text>age</text>", "<text>unknownAge</text>")));

        assertEquals(200, response.getStatus());
        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals(false, body.get("valid"));
        assertTrue(((java.util.List<?>) body.get("messages")).stream()
            .map(Object::toString)
            .anyMatch(message -> message.contains("unknownAge") || message.contains("not found")));
    }

    @Test
    void malformedBase64IsRejected() {
        Response response = resource.validate(new JitDmnResource.ValidationRequest() {{
            dmn_xml_base64 = "not-base64";
        }});

        assertEquals(400, response.getStatus());
    }

    @Test
    void compileFailureUsesStructuredUnprocessableEntityResponse() {
        JitDmnResource.JitRequest request = new JitDmnResource.JitRequest();
        request.dmn_xml_base64 = Base64.getEncoder().encodeToString(
            "<definitions xmlns=\"https://www.omg.org/spec/DMN/20211108/MODEL/\">"
                .getBytes(StandardCharsets.UTF_8));
        request.inputs = Map.of("age", 21);

        Response response = resource.evaluate(request);

        assertEquals(422, response.getStatus());
        Map<?, ?> body = (Map<?, ?>) response.getEntity();
        assertEquals("DMN compilation errors", body.get("error"));
        assertFalse(((java.util.List<?>) body.get("messages")).isEmpty());
    }

    @Test
    void malformedBase64OnEvaluateIsBadRequest() {
        JitDmnResource.JitRequest request = new JitDmnResource.JitRequest();
        request.dmn_xml_base64 = "not-base64";
        request.inputs = Map.of();

        assertEquals(400, resource.evaluate(request).getStatus());
        assertEquals(400, resource.evaluate(null).getStatus());
    }

    private static JitDmnResource.ValidationRequest request(String xml) {
        JitDmnResource.ValidationRequest request = new JitDmnResource.ValidationRequest();
        request.dmn_xml_base64 = Base64.getEncoder().encodeToString(
            xml.getBytes(StandardCharsets.UTF_8));
        return request;
    }

    private static String validModel() {
        return """
            <?xml version="1.0" encoding="UTF-8"?>
            <definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
                         id="definitions_1" name="Simple" namespace="https://example.test/simple">
              <decision id="decision_1" name="Adult status">
                <variable id="variable_1" name="Adult status" typeRef="string"/>
                <informationRequirement>
                  <requiredInput href="#inputData_1"/>
                </informationRequirement>
                <decisionTable id="table_1" hitPolicy="UNIQUE">
                  <input id="input_1">
                    <inputExpression id="expression_1" typeRef="number"><text>age</text></inputExpression>
                  </input>
                  <output id="output_1" name="status" typeRef="string"/>
                  <rule id="rule_1">
                    <inputEntry id="entry_1"><text><![CDATA[>= 18]]></text></inputEntry>
                    <outputEntry id="output_entry_1"><text><![CDATA["adult"]]></text></outputEntry>
                  </rule>
                </decisionTable>
              </decision>
              <inputData id="inputData_1" name="age">
                <variable id="input_variable_1" name="age" typeRef="number"/>
              </inputData>
            </definitions>
            """;
    }
}
