FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build
COPY pom.xml .
COPY src src
RUN mvn --batch-mode --no-transfer-progress package

FROM flink:1.20.1-scala_2.12-java17
COPY --from=build /build/target/forge-sql-runner.jar /opt/flink/usrlib/forge-sql-runner.jar
COPY --from=build /build/target/dependency/*.jar /opt/flink/lib/
RUN mkdir -p /opt/flink/plugins/s3-fs-hadoop && \
    cp /opt/flink/opt/flink-s3-fs-hadoop-*.jar /opt/flink/plugins/s3-fs-hadoop/
