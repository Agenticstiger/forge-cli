package io.fluid.forge;

import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Properties;
import org.apache.flink.table.api.EnvironmentSettings;
import org.apache.flink.table.api.TableEnvironment;
import org.apache.flink.table.types.logical.LogicalType;
import org.apache.flink.table.types.logical.RowType;
import org.apache.flink.table.types.logical.utils.LogicalTypeParser;
import org.apache.iceberg.flink.FlinkSchemaUtil;

/** Executes Forge's ordered DDL and exactly one streaming INSERT. */
public final class ForgeSqlRunner {
    private ForgeSqlRunner() {}

    public static void main(String[] args) throws Exception {
        if (args.length < 1 || args.length > 2) {
            throw new IllegalArgumentException("Expected job.properties [--validate-only]");
        }
        boolean validateOnly = args.length == 2 && "--validate-only".equals(args[1]);
        if (args.length == 2 && !validateOnly) {
            throw new IllegalArgumentException("Unknown runner option");
        }
        Properties statements = new Properties();
        try (Reader reader = Files.newBufferedReader(Path.of(args[0]), StandardCharsets.UTF_8)) {
            statements.load(reader);
        }
        int count = Integer.parseInt(statements.getProperty("count"));
        if (count < 2) {
            throw new IllegalArgumentException("Expected setup statements and one INSERT");
        }
        TableEnvironment tables = TableEnvironment.create(
                EnvironmentSettings.newInstance().inStreamingMode().build());
        for (int i = 0; i < count - 1; i++) {
            String statement = statements.getProperty("statement." + i);
            if (statement == null || !statement.startsWith("CREATE ")) {
                throw new IllegalArgumentException("Expected generated setup DDL at index " + i);
            }
            tables.executeSql(statement);
        }
        String target = statements.getProperty("expected.target");
        String expectedType = statements.getProperty("expected.type");
        if (target == null || expectedType == null) {
            throw new IllegalArgumentException("Expected a contract-bound target schema");
        }
        if (!(LogicalTypeParser.parse(expectedType) instanceof RowType declared)) {
            throw new IllegalArgumentException("Expected a ROW target schema");
        }
        // Compare Iceberg's physical representation (timestamps use microseconds).
        LogicalType expected = FlinkSchemaUtil.convert(
                FlinkSchemaUtil.convert(declared)).copy(false);
        LogicalType actual = tables.from(target).getResolvedSchema()
                .toPhysicalRowDataType().notNull().getLogicalType();
        if (!expected.equals(actual)) {
            throw new IllegalStateException(
                    "Existing Iceberg schema differs from the contract; migrate it before starting writes."
                    + " Expected: " + expected.asSerializableString()
                    + "; actual: " + actual.asSerializableString());
        }
        String insert = statements.getProperty("statement." + (count - 1));
        if (insert == null || !insert.startsWith("INSERT INTO ")) {
            throw new IllegalArgumentException("Expected generated INSERT");
        }
        // The matching Flink planner, not a generic SQL parser, is authoritative.
        // Never print the plan: catalog/source options may contain credentials.
        tables.explainSql(insert);
        if (!validateOnly) {
            tables.executeSql(insert);
        }
    }
}
