# Both base images are pinned by digest so a rebuild cannot pick up a changed tag;
# the tag stays in the reference so the digest remains readable and Dependabot's
# docker ecosystem can move the two together. The Flink tag must match
# flink.version in pom.xml.
# maven:3.9-eclipse-temurin-17
FROM maven:3.9-eclipse-temurin-26@sha256:b2c1ad85954592f9928e84327c65201f308ad9b5d8ed7d5b823717c97bf23fbb AS build
WORKDIR /build
COPY pom.xml .
COPY src src
RUN mvn --batch-mode --no-transfer-progress package

# flink:1.20.4-scala_2.12-java17
FROM flink:1.20.4-scala_2.12-java17@sha256:da329fbe16821e8b4749513f4b86eac2172f395c2d5f050017ea8396a7fa98f9
COPY --from=build /build/target/forge-sql-runner.jar /opt/flink/usrlib/forge-sql-runner.jar
COPY --from=build /build/target/dependency/*.jar /opt/flink/lib/
RUN mkdir -p /opt/flink/plugins/s3-fs-hadoop && \
    cp /opt/flink/opt/flink-s3-fs-hadoop-*.jar /opt/flink/plugins/s3-fs-hadoop/
